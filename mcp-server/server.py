"""
Wardrowbe MCP Server

Lets an MCP client (Claude, ChatGPT, ...) act as a second interface to a running
wardrowbe instance: put garments in with tags the assistant judged itself (no
Vision/LLM key needed on wardrowbe's side), browse the wardrobe, log wears and
washes, and author outfit suggestions.

Two transports:

    MCP_TRANSPORT=http   Streamable HTTP on MCP_HOST:MCP_PORT at /mcp. Several people
                         share one server; each has a Bearer token in MCP_USERS_FILE
                         that maps to their own wardrowbe account.
    MCP_TRANSPORT=stdio  (default) One local client, one account (WARDROBE_EMAIL).

Environment variables:
    WARDROBE_API_BASE      wardrowbe backend base URL, default http://127.0.0.1:8000
    WARDROBE_PUBLIC_URL    Origin clients can open; image links are rewritten to it.
    MCP_TRANSPORT          http | stdio
    MCP_HOST / MCP_PORT    Bind address for http, default 0.0.0.0:8090
    MCP_USERS_FILE         http only: JSON file of {"users": [{name, email, token}]}
    WARDROBE_EMAIL         stdio only: the account to act as
    WARDROBE_DISPLAY_NAME  stdio only: display name for a newly created account

The wear/wash/outfit/analytics tools follow the tool surface of
saya6k/mcp-wardrowbe (MIT), rewritten against this fork's API. See
THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import inspect
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Literal, Optional

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from wardrobe_client import BackendError, Identity, WardrobeBackend

TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio").lower()
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.environ.get("MCP_PORT", "8090"))
# Phone photos run 3-8 MB; the backend applies its own MAX_UPLOAD_SIZE_MB on top.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# Room for the multipart boundary and part headers around the image itself.
UPLOAD_FORM_OVERHEAD_BYTES = 64 * 1024

wardrobe = WardrobeBackend(
    os.environ.get("WARDROBE_API_BASE", "http://127.0.0.1:8000"),
    os.environ.get("WARDROBE_PUBLIC_URL", ""),
)

# Closed vocabularies the backend validates tags against
# (backend/app/data/garment_vocabulary.json). Kept here only so tool descriptions can
# tell the assistant what to send; the backend stays the judge and answers 422 with
# the offending value if this copy ever drifts.
VOCABULARY = {
    "item_type": (
        "shirt, t-shirt, top, polo, blouse, tank-top, camisole, bodysuit, sweater, "
        "sweatshirt, hoodie, cardigan, vest, jacket, coat, blazer, poncho, suit, dress, "
        "jumpsuit, pants, jeans, shorts, skirt, leggings, overalls, tights, socks, shoes, "
        "sneakers, boots, sandals, heels, flats, tie, hat, scarf, belt, bag, watch, accessories"
    ),
    "color": (
        "black, white, gray, navy, blue, light-blue, red, burgundy, pink, green, olive, "
        "yellow, orange, purple, brown, tan, beige, cream, gold, silver"
    ),
    "pattern": (
        "solid, striped, plaid, checkered, floral, graphic, geometric, polka-dot, "
        "camouflage, animal-print"
    ),
    "material": (
        "cotton, denim, leather, wool, polyester, silk, linen, knit, fleece, suede, "
        "velvet, nylon, canvas, down, shearling"
    ),
    "style": (
        "casual, classic, sporty, minimalist, bohemian, preppy, streetwear, elegant, "
        "athletic, vintage, modern, rugged"
    ),
    "season": "spring, summer, fall, winter, all-season",
    "formality": "very-casual, casual, smart-casual, business-casual, formal, very-formal",
    "fit": "slim, regular, relaxed, oversized, tailored, cropped",
    "occasion": (
        "casual, office, work, formal, smart-casual, business-casual, date, party, sporty, "
        "sport, outdoor, travel, lounge, beach, interview, wedding, dinner, brunch, gym, "
        "running, hiking, weekend"
    ),
}

_TAG_HELP = (
    "Tag values come from fixed lists. "
    + " ".join(f"{field}: {values}." for field, values in VOCABULARY.items() if field != "occasion")
)

_OUTFIT_HELP = " ".join(
    f"{field}: {VOCABULARY[field]}." for field in ("occasion", "season", "formality", "color")
)

mcp = FastMCP(
    "wardrowbe",
    instructions=(
        "Tools for the signed-in person's wardrowbe wardrobe. Each connection acts as "
        "exactly one family member; call `whoami` if it matters who.\n"
        "Adding clothes: look at the photo yourself, decide the tags, then call "
        "`add_from_product` (image is at a public URL) or `add_clothing_item` (you hold "
        "the image bytes). Call `get_status` once to learn whether wardrowbe's own AI is "
        "on; when it is off, you are the tagger and the stylist.\n"
        "Outfits: with wardrowbe's AI on, `suggest_outfit` generates one. With it off, "
        "read the wardrobe with `list_items`, pick the pieces yourself, and save the "
        "result with `create_outfit_suggestion`.\n"
        "Describe results in a sentence or two; do not paste raw JSON or image URLs "
        "unless asked."
    ),
    host=MCP_HOST,
    port=MCP_PORT,
    # No per-session state is kept, so a restart never strands a connected client and
    # each request is authenticated on its own.
    stateless_http=True,
    json_response=True,
)

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# Adds or changes data in a way that can be undone from the app; calling twice repeats it.
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)
# Same, but calling twice leaves the same state as calling once.
WRITE_IDEMPOTENT = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
# Reaches outside wardrowbe (fetches a URL, sends a notification).
WRITE_OPEN_WORLD = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


def tool(title: str, annotations: ToolAnnotations, *, appendix: str = ""):
    """Register an MCP tool whose description is its docstring plus `appendix`.

    FastMCP reads the description when the tool is registered, so shared text such
    as the tag vocabulary has to be joined on here rather than patched in later.
    """

    def register(fn):
        description = inspect.cleandoc(fn.__doc__ or "")
        if appendix:
            description = f"{description}\n\n{appendix}"
        return mcp.tool(title=title, annotations=annotations, description=description)(fn)

    return register


# ─── who is calling ──────────────────────────────────────────────────────────


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load_users() -> dict[str, Identity]:
    """Map sha256(token) -> Identity from MCP_USERS_FILE (http transport)."""
    path = os.environ.get("MCP_USERS_FILE")
    if not path:
        raise SystemExit("MCP_USERS_FILE is required when MCP_TRANSPORT=http")
    try:
        entries = json.loads(Path(path).read_text())["users"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise SystemExit(f"Cannot read users from {path}: {e}") from e
    users: dict[str, Identity] = {}
    for entry in entries:
        token = entry.get("token", "")
        if len(token) < 32:
            raise SystemExit(f"User {entry.get('name')!r}: token must be at least 32 characters")
        digest = _token_digest(token)
        if digest in users:
            raise SystemExit(f"User {entry.get('name')!r}: token is already used by another user")
        users[digest] = Identity(
            email=entry["email"], display_name=entry.get("name") or entry["email"]
        )
    if not users:
        raise SystemExit(f"{path} lists no users")
    return users


def _stdio_identity() -> Identity:
    email = os.environ.get("WARDROBE_EMAIL")
    if not email:
        raise SystemExit("WARDROBE_EMAIL is required when MCP_TRANSPORT=stdio")
    return Identity(email=email, display_name=os.environ.get("WARDROBE_DISPLAY_NAME", "MCP Server"))


USERS: dict[str, Identity] = {}
STDIO_IDENTITY: Optional[Identity] = None


def _identity_for_request(request: Request) -> Optional[Identity]:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return USERS.get(_token_digest(token.strip()))


def _identity(ctx: Context) -> Identity:
    """The wardrowbe account this tool call acts as."""
    if STDIO_IDENTITY is not None:
        return STDIO_IDENTITY
    request = ctx.request_context.request
    identity = _identity_for_request(request) if request is not None else None
    if identity is None:
        # BearerAuth already rejected unknown tokens; this guards a tool call that
        # somehow arrives without its HTTP request attached.
        raise PermissionError("No signed-in user for this request")
    return identity


class BearerAuth:
    """Reject any HTTP request that does not carry a known user's Bearer token."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] == "/health":
            await self.app(scope, receive, send)
            return
        if _identity_for_request(Request(scope)) is None:
            response = JSONResponse(
                {"error": "unauthorized", "detail": "Send your personal token as 'Authorization: Bearer <token>'"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


# ─── helpers ─────────────────────────────────────────────────────────────────


async def _call(ctx: Context, method: str, path: str, **kwargs: Any) -> Any:
    return await wardrobe.request(_identity(ctx), method, path, **kwargs)


def _item_form(
    item_type: str,
    name: str,
    brand: str,
    notes: str,
    color: str,
    colors: Optional[list[str]],
    style: Optional[list[str]],
    season: Optional[list[str]],
    material: str,
    pattern: str,
    formality: str,
    fit: str,
    source_url: str = "",
    subtype: str = "",
    size: str = "",
    care_instructions: str = "",
) -> dict[str, str]:
    """Form fields for POST /api/v1/items with caller-judged tags (skip_ai=true)."""
    fields = {
        "type": item_type,
        "subtype": subtype,
        "name": name,
        "brand": brand,
        "notes": notes,
        "primary_color": color,
        "colors": ",".join(colors or []),
        "style": ",".join(style or []),
        "season": ",".join(season or []),
        "material": material,
        "pattern": pattern,
        "formality": formality,
        "fit": fit,
        "source_url": source_url,
        "size": size,
        "care_instructions": care_instructions,
    }
    return {"skip_ai": "true", **{key: value for key, value in fields.items() if value}}


def _image_content_type(filename: str) -> str:
    # The backend checks the MIME type against an allowlist before opening the
    # bytes, so a generic octet-stream upload is rejected.
    return mimetypes.guess_type(filename)[0] or "image/jpeg"


def _resolve_image_bytes(image_path: str, image_base64: str, filename: str) -> tuple[bytes, str]:
    """Read image bytes from exactly one of a local path or base64, returning (content, filename).

    image_path is only honored over stdio (one local client, one filesystem); over
    HTTP there is no shared filesystem to resolve it against.
    """
    if bool(image_path) == bool(image_base64):
        raise ValueError("Provide exactly one of image_path or image_base64")
    if image_path:
        if STDIO_IDENTITY is None:
            raise ValueError(
                "image_path is only available over stdio; over HTTP send image_base64, "
                "image_url, or use POST /upload"
            )
        path = Path(image_path).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"No such image file: {path}")
        return path.read_bytes(), path.name
    try:
        return base64.b64decode(image_base64, validate=True), filename
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"image_base64 is not valid base64: {e}") from e


async def _attach_extra_images(
    ctx: Context,
    item_id: str,
    extra_image_paths: Optional[list[str]],
    extra_image_urls: Optional[list[str]],
    extra_image_base64: Optional[list[str]] = None,
) -> None:
    """Add additional photos (beyond the primary one) to a just-created item, in order.

    Each call is independent so one bad path, URL or base64 blob does not lose the
    others; the item itself already exists by the time this runs.
    """
    for path in extra_image_paths or []:
        content, filename = _resolve_image_bytes(path, "", "photo.jpg")
        await _call(
            ctx, "POST", f"/items/{item_id}/images",
            files={"image": (filename, content, _image_content_type(filename))},
        )
    for url in extra_image_urls or []:
        await _call(ctx, "POST", f"/items/{item_id}/images", data={"image_url": url})
    for blob in extra_image_base64 or []:
        content, filename = _resolve_image_bytes("", blob, "photo.jpg")
        await _call(
            ctx, "POST", f"/items/{item_id}/images",
            files={"image": (filename, content, _image_content_type(filename))},
        )


# ─── status ──────────────────────────────────────────────────────────────────


@tool("Server status", READ)
async def get_status() -> dict[str, Any]:
    """Report whether wardrowbe is up and which AI work it leaves to you.

    `ai.vision=false` means wardrowbe will not tag photos itself: send tags with every
    item you add. `ai.text=false` means `suggest_outfit` is unavailable: compose outfits
    yourself and save them with `create_outfit_suggestion`.
    """
    capabilities = await wardrobe.public_get("/capabilities")
    return {"healthy": True, **capabilities}


@tool("Who am I", READ)
async def whoami(ctx: Context) -> dict[str, Any]:
    """Return the wardrowbe account this connection acts as (name, email, family)."""
    return await _call(ctx, "GET", "/auth/session")


# ─── items: read ─────────────────────────────────────────────────────────────


@tool("List wardrobe items", READ)
async def list_items(
    ctx: Context,
    search: str = "",
    item_type: str = "",
    colors: Optional[list[str]] = None,
    favorite: Optional[bool] = None,
    needs_wash: Optional[bool] = None,
    is_archived: bool = False,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """List or search the wardrobe. All filters are optional and combine with AND.

    Args:
        search: Free text matched against name, brand and notes.
        item_type: Exact garment type, e.g. "jeans".
        colors: Only items in any of these colors.
        favorite: true for favorites only.
        needs_wash: true for items due a wash.
        is_archived: true to list archived items instead of active ones.
        page: 1-based page number.
        page_size: 1-100.

    Returns {items, total, page, page_size, has_more}.
    """
    return await _call(
        ctx,
        "GET",
        "/items",
        params={
            "search": search or None,
            "type": item_type or None,
            "colors": ",".join(colors) if colors else None,
            "favorite": favorite,
            "needs_wash": needs_wash,
            "is_archived": is_archived,
            "page": page,
            "page_size": max(1, min(page_size, 100)),
        },
    )


@tool("Get one item", READ)
async def get_item(ctx: Context, item_id: str) -> dict[str, Any]:
    """Fetch one wardrobe item by id, with all its tags and wear counters."""
    return await _call(ctx, "GET", f"/items/{item_id}")


@tool("Wardrobe statistics", READ)
async def get_wardrobe_summary(ctx: Context, days: int = 30) -> dict[str, Any]:
    """Wardrobe analytics: item counts by type and color, most and least worn items,
    outfit acceptance rate, and wear trends over the last `days` days (7-365)."""
    return await _call(ctx, "GET", "/analytics", params={"days": max(7, min(days, 365))})


# ─── items: write ────────────────────────────────────────────────────────────


@tool("Add item from an image you hold", WRITE, appendix=_TAG_HELP)
async def add_clothing_item(
    ctx: Context,
    item_type: str,
    image_base64: str = "",
    image_filename: str = "photo.jpg",
    image_path: str = "",
    name: str = "",
    brand: str = "",
    subtype: str = "",
    color: str = "",
    colors: Optional[list[str]] = None,
    style: Optional[list[str]] = None,
    season: Optional[list[str]] = None,
    material: str = "",
    pattern: str = "",
    formality: str = "",
    fit: str = "",
    size: str = "",
    care_instructions: str = "",
    notes: str = "",
    extra_image_paths: Optional[list[str]] = None,
    extra_image_urls: Optional[list[str]] = None,
    extra_image_base64: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Add a garment from a photo you have looked at, with the tags you judged yourself.

    wardrowbe's own AI tagging is skipped, so fill in every tag you can see.

    Give the primary image one way:
      - image_base64: the file's bytes, base64-encoded (JPEG, PNG, WebP or HEIC).
        Practical only for small images.
      - image_path: a path on the machine running this server. Only accepted over
        the stdio transport.
    For a full-size photo on the user's machine when connected over HTTP, upload it
    out of band instead: `curl -H "Authorization: Bearer <token>" -F image=@photo.jpg
    <server>/upload` creates the item untagged and returns its id; then call
    `update_item` with the tags.

    extra_image_paths / extra_image_urls / extra_image_base64 add up to 3 more
    photos (e.g. a price tag, a back view) to the same item in one call, 4 total.
    Each is attached after the item is created, in list order; a bad one is skipped
    rather than failing the whole call. image_path entries follow the same
    stdio-only rule as above; extra_image_base64 is the one that works over HTTP
    for a photo you (not the server) hold.

    Args:
        item_type: Garment type.
        image_filename: Original file name; its extension tells the image format.
        name: Short name, e.g. "Navy oxford shirt".
        subtype: Free text, e.g. "blouson" for a jacket.
        color: The single dominant color.
        colors: All visible colors, dominant first.
        style, season: Lists, e.g. ["casual", "minimalist"], ["fall", "winter"].
        size: Free text, e.g. "M" or "32x30".
        care_instructions: Free text from a care label, if visible.
    """
    content, image_filename = _resolve_image_bytes(image_path, image_base64, image_filename)

    item = await _call(
        ctx,
        "POST",
        "/items",
        data=_item_form(
            item_type, name, brand, notes, color, colors, style, season, material, pattern, formality, fit,
            subtype=subtype, size=size, care_instructions=care_instructions,
        ),
        files={"image": (image_filename, content, _image_content_type(image_filename))},
    )
    if extra_image_paths or extra_image_urls or extra_image_base64:
        await _attach_extra_images(ctx, item["id"], extra_image_paths, extra_image_urls, extra_image_base64)
        item = await _call(ctx, "GET", f"/items/{item['id']}")
    return item



@tool("Add item from a product image URL", WRITE_OPEN_WORLD, appendix=_TAG_HELP)
async def add_from_product(
    ctx: Context,
    image_url: str,
    item_type: str,
    source_url: str = "",
    name: str = "",
    brand: str = "",
    subtype: str = "",
    color: str = "",
    colors: Optional[list[str]] = None,
    style: Optional[list[str]] = None,
    season: Optional[list[str]] = None,
    material: str = "",
    pattern: str = "",
    formality: str = "",
    fit: str = "",
    size: str = "",
    care_instructions: str = "",
    notes: str = "",
    extra_image_paths: Optional[list[str]] = None,
    extra_image_urls: Optional[list[str]] = None,
    extra_image_base64: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Add a garment from a product photo on the web, with the tags you judged yourself.

    wardrowbe downloads the primary image itself, so use this whenever that picture
    is at a public http(s) URL (a shop's product photo). Private, loopback and
    redirecting URLs are refused.

    extra_image_paths / extra_image_urls / extra_image_base64 add up to 3 more
    photos (e.g. the user's own photo of the item, or its price tag) to the same
    item in one call, 4 total. Each is attached after the item is created, in list
    order; a bad one is skipped rather than failing the whole call. A path is only
    honored over stdio; extra_image_base64 is the one that works over HTTP for a
    photo you (not the server) hold.

    Args:
        image_url: Direct URL of the primary image file, not of the product page.
        item_type: Garment type.
        source_url: The product page; kept on the item so it can be bought again.
        subtype: Free text, e.g. "blouson" for a jacket.
        size: Free text, e.g. "M" or "32x30".
        care_instructions: Free text from a care label, if visible.
        name, brand, color, colors, style, season, material, pattern, formality, fit,
        notes: As in `add_clothing_item`.
    """
    item = await _call(
        ctx,
        "POST",
        "/items",
        data={
            "image_url": image_url,
            **_item_form(
                item_type, name, brand, notes, color, colors, style, season, material, pattern, formality, fit,
                source_url=source_url, subtype=subtype, size=size, care_instructions=care_instructions,
            ),
        },
    )
    if extra_image_paths or extra_image_urls or extra_image_base64:
        await _attach_extra_images(ctx, item["id"], extra_image_paths, extra_image_urls, extra_image_base64)
        item = await _call(ctx, "GET", f"/items/{item['id']}")
    return item



@tool("Edit an item's details and tags", WRITE_IDEMPOTENT, appendix=_TAG_HELP)
async def update_item(
    ctx: Context,
    item_id: str,
    item_type: Optional[str] = None,
    subtype: Optional[str] = None,
    name: Optional[str] = None,
    brand: Optional[str] = None,
    notes: Optional[str] = None,
    favorite: Optional[bool] = None,
    color: Optional[str] = None,
    colors: Optional[list[str]] = None,
    style: Optional[list[str]] = None,
    season: Optional[list[str]] = None,
    material: Optional[str] = None,
    pattern: Optional[str] = None,
    formality: Optional[str] = None,
    fit: Optional[str] = None,
    size: Optional[str] = None,
    care_instructions: Optional[str] = None,
    source_url: Optional[str] = None,
    purchase_price: Optional[float] = None,
    purchase_date: Optional[str] = None,
    wash_interval: Optional[int] = None,
) -> dict[str, Any]:
    """Change an existing item. Only the fields you pass are changed; the rest keep
    their values. Use this to tag an item that was uploaded untagged, or to correct
    a wrong tag.

    Args:
        item_id: The item to change.
        item_type: Garment type.
        color: The single dominant color.
        colors: All visible colors, dominant first.
        size: Free text, e.g. "M" or "32x30".
        purchase_price: What the user paid, in their local currency's smallest
            common unit (e.g. 1290 for NT$1,290), no currency symbol.
        purchase_date: YYYY-MM-DD; leave unset if the purchase isn't confirmed/dated.
        wash_interval: Wears between washes. Leave unset to keep wardrowbe's default
            for the item's type.
    """
    fields = {
        "type": item_type,
        "subtype": subtype,
        "name": name,
        "brand": brand,
        "notes": notes,
        "favorite": favorite,
        "primary_color": color,
        "colors": colors,
        "purchase_price": purchase_price,
        "purchase_date": purchase_date,
        "wash_interval": wash_interval,
    }
    tags = {
        "style": style,
        "season": season,
        "material": material,
        "pattern": pattern,
        "formality": formality,
        "fit": fit,
        "size": size,
        "care_instructions": care_instructions,
        "source_url": source_url,
    }
    body: dict[str, Any] = {key: value for key, value in fields.items() if value is not None}
    tags = {key: value for key, value in tags.items() if value is not None}
    if tags:
        body["tags"] = tags
    if not body:
        raise ValueError("Pass at least one field to change")
    return await _call(ctx, "PATCH", f"/items/{item_id}", json=body)



@tool("Add an extra photo to an item", WRITE_OPEN_WORLD)
async def add_item_image(
    ctx: Context,
    item_id: str,
    image_path: str = "",
    image_base64: str = "",
    image_filename: str = "photo.jpg",
    image_url: str = "",
) -> dict[str, Any]:
    """Attach one more photo to an existing item (e.g. a price tag, a back view),
    up to 4 photos total including the primary one. The original image is kept as
    uploaded; wardrowbe does not crop or remove its background.

    Give the image exactly one way:
      - image_path: a path on the machine running this server. Only accepted over
        the stdio transport.
      - image_base64: the file's bytes, base64-encoded (JPEG, PNG, WebP or HEIC).
      - image_url: a public http(s) URL; wardrowbe downloads it itself. Private,
        loopback and redirecting URLs are refused.

    Returns the new image's id and signed URLs. Call `get_item` to see it listed
    under the item's `additional_images`.
    """
    provided = (bool(image_path), bool(image_base64), bool(image_url))
    if sum(provided) != 1:
        raise ValueError("Provide exactly one of image_path, image_base64 or image_url")
    if image_url:
        return await _call(ctx, "POST", f"/items/{item_id}/images", data={"image_url": image_url})
    content, image_filename = _resolve_image_bytes(image_path, image_base64, image_filename)
    return await _call(
        ctx,
        "POST",
        f"/items/{item_id}/images",
        files={"image": (image_filename, content, _image_content_type(image_filename))},
    )


@tool("Log that an item was worn", WRITE)
async def log_wear(
    ctx: Context, item_id: str, worn_at: str = "", occasion: str = "", notes: str = ""
) -> dict[str, Any]:
    """Record one wear of an item.

    Args:
        worn_at: YYYY-MM-DD; defaults to today in the user's timezone.
        occasion: Optional, one of the occasion values listed in `create_outfit_suggestion`.
    """
    body = {"worn_at": worn_at, "occasion": occasion, "notes": notes}
    return await _call(ctx, "POST", f"/items/{item_id}/wear", json={k: v for k, v in body.items() if v})


@tool("Log that an item was washed", WRITE)
async def log_wash(ctx: Context, item_id: str, washed_at: str = "", notes: str = "") -> dict[str, Any]:
    """Mark an item as washed, resetting its wears-since-wash counter. Fails if the
    item has not been worn since its last wash.

    Args:
        washed_at: YYYY-MM-DD; defaults to today in the user's timezone.
    """
    body = {"washed_at": washed_at, "notes": notes}
    return await _call(ctx, "POST", f"/items/{item_id}/wash", json={k: v for k, v in body.items() if v})


@tool("Archive an item", WRITE_IDEMPOTENT)
async def archive_item(ctx: Context, item_id: str, reason: str = "") -> dict[str, Any]:
    """Hide an item from the active wardrobe without deleting it (donated, stored
    away, worn out). `restore_item` brings it back.

    Args:
        reason: Optional short reason, up to 50 characters.
    """
    return await _call(ctx, "POST", f"/items/{item_id}/archive", json={"reason": reason} if reason else {})


@tool("Restore an archived item", WRITE_IDEMPOTENT)
async def restore_item(ctx: Context, item_id: str) -> dict[str, Any]:
    """Return an archived item to the active wardrobe."""
    return await _call(ctx, "POST", f"/items/{item_id}/restore")


# ─── outfits ─────────────────────────────────────────────────────────────────


@tool("Ask wardrowbe's AI for an outfit", WRITE)
async def suggest_outfit(
    ctx: Context,
    occasion: str = "",
    time_of_day: Literal["", "morning", "afternoon", "evening", "night", "full day"] = "",
    include_items: Optional[list[str]] = None,
    exclude_items: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Have wardrowbe's own AI generate an outfit from the wardrobe and today's weather.

    Only works when `get_status` reports `ai.text=true`. When it is false this fails
    with "Internal AI is disabled": pick the items yourself and call
    `create_outfit_suggestion` instead.

    Args:
        occasion: One of the occasion values listed in `create_outfit_suggestion`;
            defaults to the user's usual occasion.
        include_items: Item ids the outfit must contain.
        exclude_items: Item ids the outfit must not contain.
    """
    body: dict[str, Any] = {
        "occasion": occasion,
        "time_of_day": time_of_day,
        "include_items": include_items,
        "exclude_items": exclude_items,
    }
    return await _call(ctx, "POST", "/outfits/suggest", json={k: v for k, v in body.items() if v})


@tool("Save an outfit you composed", WRITE, appendix=_OUTFIT_HELP)
async def create_outfit_suggestion(
    ctx: Context,
    item_ids: list[str],
    occasion: str,
    name: str = "",
    reasoning: str = "",
    style_notes: str = "",
    scheduled_for: str = "",
    season: str = "",
    formality: str = "",
    palette: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Save an outfit you put together from the user's own items. It appears in
    wardrowbe as a pending suggestion the user can accept, reject or skip. Works
    whether or not wardrowbe's own AI is enabled.

    Args:
        item_ids: 1-20 ids of items from `list_items`, all belonging to this user.
        occasion: Required; see the occasion list below.
        name: Short title, e.g. "Rainy Friday at the office".
        reasoning: Why these pieces work together; shown to the user.
        style_notes: Styling tips, e.g. "tuck the shirt, roll the cuffs".
        scheduled_for: YYYY-MM-DD the outfit is for; defaults to today.
        season, formality: Optional; see the lists below.
        palette: Dominant outfit colors, most prominent first.
    """
    body: dict[str, Any] = {
        "items": item_ids,
        "occasion": occasion,
        "name": name,
        "reasoning": reasoning,
        "style_notes": style_notes,
        "scheduled_for": scheduled_for,
        "season": season,
        "formality": formality,
        "palette": palette,
    }
    return await _call(ctx, "POST", "/outfits/suggestions", json={k: v for k, v in body.items() if v})



@tool("List outfits", READ)
async def list_outfits(
    ctx: Context,
    status: Literal["", "pending", "sent", "accepted", "rejected", "skipped"] = "",
    occasion: str = "",
    date_from: str = "",
    date_to: str = "",
    page: int = 1,
    page_size: int = 10,
) -> dict[str, Any]:
    """List the user's outfits, newest first.

    Args:
        status: Only outfits in this state. "pending" and "sent" are awaiting a decision.
        date_from, date_to: YYYY-MM-DD bounds on the day the outfit is for.
        page_size: 1-100.

    Returns {outfits, total, page, page_size, has_more}.
    """
    return await _call(
        ctx,
        "GET",
        "/outfits",
        params={
            "status": status or None,
            "occasion": occasion or None,
            "date_from": date_from or None,
            "date_to": date_to or None,
            "page": page,
            "page_size": max(1, min(page_size, 100)),
        },
    )


@tool("Get one outfit", READ)
async def get_outfit(ctx: Context, outfit_id: str) -> dict[str, Any]:
    """Fetch one outfit by id, with its full item list and any feedback."""
    return await _call(ctx, "GET", f"/outfits/{outfit_id}")


@tool("Accept, reject or skip an outfit", WRITE_IDEMPOTENT)
async def respond_to_outfit(
    ctx: Context, action: Literal["accept", "reject", "skip"], outfit_id: str = ""
) -> dict[str, Any]:
    """Record the user's decision on a suggested outfit.

    Args:
        action: "accept" (will wear it), "reject" (does not like it) or "skip" (not today).
        outfit_id: The outfit to act on. Leave empty to act on the newest outfit that
            is still awaiting a decision.
    """
    if not outfit_id:
        for status in ("pending", "sent"):
            page = await _call(ctx, "GET", "/outfits", params={"status": status, "page_size": 1})
            if page["outfits"]:
                outfit_id = page["outfits"][0]["id"]
                break
        else:
            raise ValueError("No outfit is awaiting a decision")
    return await _call(ctx, "POST", f"/outfits/{outfit_id}/{action}")


@tool("Rate an outfit", WRITE_IDEMPOTENT)
async def submit_outfit_feedback(
    ctx: Context,
    outfit_id: str,
    rating: Optional[int] = None,
    comfort_rating: Optional[int] = None,
    style_rating: Optional[int] = None,
    worn: Optional[bool] = None,
    comment: str = "",
) -> dict[str, Any]:
    """Save the user's feedback on an outfit; wardrowbe learns preferences from it.
    Calling again for the same outfit updates the earlier feedback.

    Args:
        rating, comfort_rating, style_rating: 1 (bad) to 5 (great).
        worn: Whether the user actually wore it.
        comment: Free text, up to 1000 characters.
    """
    body: dict[str, Any] = {
        "rating": rating,
        "comfort_rating": comfort_rating,
        "style_rating": style_rating,
        "worn": worn,
        "comment": comment or None,
    }
    body = {key: value for key, value in body.items() if value is not None}
    if not body:
        raise ValueError("Pass at least one of rating, comfort_rating, style_rating, worn, comment")
    return await _call(ctx, "POST", f"/outfits/{outfit_id}/feedback", json=body)


# ─── notifications ───────────────────────────────────────────────────────────


@tool("Recent notifications", READ)
async def list_notifications(ctx: Context, limit: int = 20) -> list[dict[str, Any]]:
    """List the notifications wardrowbe recently sent this user (outfit reminders and
    the like), newest first. `limit` 1-100."""
    return await _call(ctx, "GET", "/notifications/history", params={"limit": max(1, min(limit, 100))})


@tool("Send a test notification", WRITE_OPEN_WORLD)
async def test_notification(ctx: Context, setting_id: str) -> dict[str, Any]:
    """Send a real test message through one of the user's notification channels
    (ntfy, email, ...). `setting_id` is a notification setting's id."""
    return await _call(ctx, "POST", f"/notifications/settings/{setting_id}/test")


# ─── plain HTTP routes (http transport only) ─────────────────────────────────


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    """Unauthenticated liveness probe; says nothing about users or data."""
    try:
        await wardrobe.public_get("/health")
    except BackendError:
        return JSONResponse({"status": "degraded", "backend": "unreachable"}, status_code=503)
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/upload", methods=["POST"])
async def upload(request: Request) -> JSONResponse:
    """Create an untagged item from a multipart `image` field.

    A full-size photo is megabytes of base64, which an assistant cannot sensibly
    pass through a tool argument. So the bytes travel here over plain HTTP, and the
    assistant then tags the item with `update_item`.
    """
    identity = _identity_for_request(request)
    if identity is None:  # BearerAuth already checked; kept so the route is safe on its own.
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    too_large = JSONResponse({"error": "Image is larger than 25 MB"}, status_code=413)
    # Refuse before reading anything. The form parser spools the whole body to
    # disk, so a request with no declared length could otherwise grow without
    # bound; the HTTP server holds a request to the length it declares.
    declared = request.headers.get("content-length", "")
    if not declared.isdigit():
        return JSONResponse({"error": "Content-Length is required"}, status_code=411)
    if int(declared) > MAX_UPLOAD_BYTES + UPLOAD_FORM_OVERHEAD_BYTES:
        return too_large
    form = await request.form(max_files=1, max_fields=8)
    image = form.get("image")
    if image is None or isinstance(image, str):
        return JSONResponse({"error": "Send the photo as a multipart field named 'image'"}, status_code=400)
    body = bytearray()
    while chunk := await image.read(1024 * 1024):
        body.extend(chunk)
        if len(body) > MAX_UPLOAD_BYTES:
            return too_large
    content = bytes(body)
    filename = image.filename or "photo.jpg"
    try:
        item = await wardrobe.request(
            identity,
            "POST",
            "/items",
            data={"skip_ai": "true"},
            files={"image": (filename, content, image.content_type or _image_content_type(filename))},
        )
    except BackendError as e:
        return JSONResponse({"error": str(e)}, status_code=502)
    return JSONResponse(
        {"item_id": item["id"], "next": "Call update_item with this item_id to add its tags.", "item": item},
        status_code=201,
    )


def http_app() -> ASGIApp:
    return BearerAuth(mcp.streamable_http_app())


if __name__ == "__main__":
    if TRANSPORT == "http":
        import uvicorn

        USERS.update(_load_users())
        uvicorn.run(http_app(), host=MCP_HOST, port=MCP_PORT, log_level="info")
    elif TRANSPORT == "stdio":
        STDIO_IDENTITY = _stdio_identity()
        mcp.run()
    else:
        raise SystemExit(f"MCP_TRANSPORT must be 'http' or 'stdio', not {TRANSPORT!r}")

"""
End-to-end smoke test against a running MCP server, over the real protocol
(Streamable HTTP + Bearer token), so it exercises exactly what a client does.

    MCP_URL=http://<host>:8090 MCP_TOKEN=<token> python3 smoke_test.py photo1.png photo2.png

Optional: MCP_TOKEN_2=<another user's token> also checks that the two tokens land
in separate wardrobes. PRODUCT_IMAGE_URL / PRODUCT_SOURCE_URL override the product
used for add_from_product.

It creates real items and an outfit in MCP_TOKEN's wardrobe; use a throwaway user.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:8090").rstrip("/")
TOKEN = os.environ["MCP_TOKEN"]
TOKEN_2 = os.environ.get("MCP_TOKEN_2")
PRODUCT_IMAGE_URL = os.environ.get(
    "PRODUCT_IMAGE_URL", "https://www.uniqlo.com/tw/hmall/test/u0000000054853/main/other6/480/7.jpg"
)
PRODUCT_SOURCE_URL = os.environ.get(
    "PRODUCT_SOURCE_URL", "https://www.uniqlo.com/tw/zh_TW/product-detail.html?productCode=u0000000054853"
)

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{' -- ' + detail if detail else ''}")
    if not ok:
        failures.append(label)


class Client:
    def __init__(self, session: ClientSession) -> None:
        self.session = session

    async def call(self, tool: str, **arguments: Any) -> tuple[bool, Any]:
        """Returns (succeeded, decoded result or error text)."""
        result = await self.session.call_tool(tool, arguments)
        text = result.content[0].text if result.content else ""
        if result.isError:
            return False, text
        if result.structuredContent is not None:
            data = result.structuredContent
            return True, data.get("result", data) if set(data) == {"result"} else data
        return True, json.loads(text) if text else None


def connect(token: str):
    return streamablehttp_client(f"{MCP_URL}/mcp", headers={"Authorization": f"Bearer {token}"})


async def main(photos: list[Path]) -> None:
    print(f"MCP server: {MCP_URL}")
    async with httpx.AsyncClient(timeout=60) as http:
        print("\n1. Access control")
        r = await http.get(f"{MCP_URL}/health")
        check("GET /health is open", r.status_code == 200, r.text)
        r = await http.post(f"{MCP_URL}/mcp", json={})
        check("POST /mcp without a token is refused", r.status_code == 401, str(r.status_code))
        r = await http.post(f"{MCP_URL}/mcp", json={}, headers={"Authorization": "Bearer " + "0" * 64})
        check("POST /mcp with an unknown token is refused", r.status_code == 401, str(r.status_code))
        r = await http.post(f"{MCP_URL}/upload", files={"image": ("a.png", b"x", "image/png")})
        check("POST /upload without a token is refused", r.status_code == 401, str(r.status_code))

        print("\n2. Upload a full-size photo over plain HTTP, then tag it")
        r = await http.post(
            f"{MCP_URL}/upload",
            headers={"Authorization": f"Bearer {TOKEN}"},
            files={"image": (photos[0].name, photos[0].read_bytes(), "image/png")},
        )
        check("POST /upload creates an item", r.status_code == 201, r.text[:200])
        uploaded_id = r.json().get("item_id") if r.status_code == 201 else None

    async with connect(TOKEN) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            c = Client(session)

            tools = (await session.list_tools()).tools
            unannotated = [t.name for t in tools if not t.title or t.annotations is None or t.annotations.readOnlyHint is None]
            check(f"{len(tools)} tools listed, all with a title and read/write annotation", not unannotated, str(unannotated))

            ok, status = await c.call("get_status")
            check("get_status", ok, json.dumps(status.get("ai")) if ok else status)
            internal_ai = bool(ok and status["ai"]["text"])
            ok, me = await c.call("whoami")
            check("whoami", ok, f"{me.get('display_name')} <{me.get('email')}>" if ok else me)

            if uploaded_id:
                ok, item = await c.call(
                    "update_item", item_id=uploaded_id, item_type="pants", name="Smoke-test trousers",
                    color="tan", colors=["tan"], style=["casual", "classic"], season=["fall", "winter"],
                    material="cotton", pattern="solid", formality="smart-casual", fit="relaxed", size="M",
                )
                check("update_item tags the uploaded item", ok and item["type"] == "pants" and item["tags"].get("material") == "cotton", "" if ok else item)
                ok, item = await c.call("update_item", item_id=uploaded_id, fit="baggy")
                check("update_item rejects an off-vocabulary tag", not ok, str(item)[:120])

            print("\n3. Add items through tools")
            ok, second = await c.call(
                "add_clothing_item", item_type="hoodie", name="Smoke-test hoodie",
                image_base64=base64.b64encode(photos[1].read_bytes()).decode(), image_filename=photos[1].name,
                color="gray", style=["casual", "streetwear"], season=["fall"], material="fleece",
                pattern="solid", formality="casual", fit="oversized",
            )
            check("add_clothing_item (image_base64)", ok and second["tags"].get("fit") == "oversized", "" if ok else second)
            ok, err = await c.call("add_clothing_item", item_type="hoodie", image_path="/etc/hosts")
            check("add_clothing_item refuses image_path over HTTP", not ok, str(err)[:90])

            ok, product = await c.call(
                "add_from_product", image_url=PRODUCT_IMAGE_URL, source_url=PRODUCT_SOURCE_URL,
                item_type="pants", name="Smoke-test product", color="brown", material="cotton",
            )
            check("add_from_product keeps source_url", ok and product["tags"].get("source_url") == PRODUCT_SOURCE_URL, "" if ok else str(product)[:200])
            ok, err = await c.call("add_from_product", image_url="http://127.0.0.1:8000/x.jpg", item_type="t-shirt")
            check("add_from_product refuses a loopback URL", not ok, str(err)[:90])

            print("\n4. Browse")
            ok, listing = await c.call("list_items", search="Smoke-test")
            check("list_items search", ok and listing["total"] >= 2, f"total={listing.get('total')}" if ok else listing)
            ids = [i["id"] for i in listing["items"]] if ok else []
            if ids:
                ok, item = await c.call("get_item", item_id=ids[0])
                image_url = (item.get("image_url") or "") if ok else ""
                check("get_item returns an absolute image link", ok and image_url.startswith("http"), image_url[:80] if ok else item)
            ok, summary = await c.call("get_wardrobe_summary")
            check("get_wardrobe_summary", ok, "" if ok else summary)

            print("\n5. Outfits")
            ok, outfit = await c.call(
                "create_outfit_suggestion", item_ids=ids[:2], occasion="casual", name="Smoke-test outfit",
                reasoning="Relaxed neutrals for a cool day.", season="fall", formality="casual",
            )
            check("create_outfit_suggestion", ok and len(outfit.get("items", [])) == len(ids[:2]), "" if ok else outfit)
            ok, pending = await c.call("list_outfits", status="pending")
            check("list_outfits shows it pending", ok and pending["total"] >= 1, "" if ok else pending)
            ok, decided = await c.call("respond_to_outfit", action="accept")
            check("respond_to_outfit accepts the newest pending outfit", ok, "" if ok else decided)
            if outfit and isinstance(outfit, dict) and outfit.get("id"):
                ok, got = await c.call("get_outfit", outfit_id=outfit["id"])
                check("get_outfit shows it accepted", ok and got["status"] == "accepted", got.get("status") if ok else got)
                ok, fb = await c.call("submit_outfit_feedback", outfit_id=outfit["id"], rating=4, worn=True, comment="smoke test")
                check("submit_outfit_feedback", ok, "" if ok else fb)
            ok, generated = await c.call("suggest_outfit", occasion="casual")
            if internal_ai:
                check("suggest_outfit (internal AI on)", ok, "" if ok else str(generated)[:160])
            else:
                check("suggest_outfit says internal AI is off", not ok and "disabled" in str(generated), str(generated)[:120])

            print("\n6. Wear, wash, archive")
            if ids:
                ok, worn = await c.call("log_wear", item_id=ids[0], occasion="casual")
                check("log_wear", ok and worn["wear_count"] >= 1, "" if ok else worn)
                ok, washed = await c.call("log_wash", item_id=ids[0])
                check("log_wash", ok and washed["wears_since_wash"] == 0, "" if ok else washed)
                ok, archived = await c.call("archive_item", item_id=ids[0], reason="smoke test")
                check("archive_item", ok and archived["is_archived"], "" if ok else archived)
                ok, restored = await c.call("restore_item", item_id=ids[0])
                check("restore_item", ok and not restored["is_archived"], "" if ok else restored)
            ok, notifications = await c.call("list_notifications")
            check("list_notifications", ok, "" if ok else notifications)

    if TOKEN_2:
        print("\n7. A second token is a different person")
        async with connect(TOKEN_2) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                c = Client(session)
                ok, other = await c.call("whoami")
                check("whoami differs", ok and other["email"] != me["email"], other.get("email") if ok else other)
                ok, listing = await c.call("list_items", search="Smoke-test")
                check("the first user's items are not visible", ok and listing["total"] == 0, f"total={listing.get('total')}" if ok else listing)
                if ids:
                    ok, item = await c.call("get_item", item_id=ids[0])
                    check("nor fetchable by id", not ok, str(item)[:80])

    print(f"\n{'ALL PASSED' if not failures else 'FAILED: ' + ', '.join(failures)}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: smoke_test.py <photo1> <photo2>   (two different garment photos)")
    asyncio.run(main([Path(sys.argv[1]), Path(sys.argv[2])]))

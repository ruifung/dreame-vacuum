"""De-obfuscated version of frontend.py (for analysis/reading only, not wired up).

Serves the Dreame Vacuum Card Lovelace frontend and runs the DVC (Dreame Vacuum
Card) license key distribution protocol against a remote license server.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import re
from pathlib import Path

import aiohttp
from aiohttp import web
from Crypto.Cipher import AES, ChaCha20
from Crypto.Random import get_random_bytes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from homeassistant.components.http import HomeAssistantView
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.helpers import device_registry
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.storage import Store

from .const import CONF_DVC_KEY, DOMAIN, FRONTEND, LOGGER
from .dreame import VERSION


def _hex(s: str) -> bytes:
    return bytes.fromhex(s)


def _b64_hex(s: str) -> bytes:
    return bytes.fromhex(base64.b64decode(s).decode())


# Fixed X25519 public key of the license server (hex).
SERVER_X25519_PUB = bytes.fromhex(
    "daaf3e8e82dc8122e70af70cd22337993950cae8ecbbef484b0926c13cb48058"
)
# Fixed Ed25519 public key used to verify license payloads (hex).
LICENSE_ED25519_PUB = bytes.fromhex(
    "5c2e425b33b51831fc3f3caee6a77237b1dc673bf9a426e1fb5234e4768a402d"
)

# Files whose normalized content is hashed for integrity / versioning.
SOURCE_FILES = (
    "coordinator.py",
    "dreame/__init__.py",
    "dreame/device.py",
    "dreame/protocol.py",
    "entity.py",
    "frontend.py",
    "vacuum.py",
)

LICENSE_SERVER = "https://api.tasshack.com/get"
DVC_PREFIX = "dvc-token-v1:"
USER_DATA_PREFIX = "frontend_user_data_"
DVC_MARKER = "dvc:"
CACHE_MAP_KEY = "zk9fpqx2"

# UUID v4 regex used to validate the DVC key format.
KEY_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def _source_hash() -> str:
    """sha256 over concatenated, normalized (CRLF->LF) source files."""
    data = b""
    for name in SOURCE_FILES:
        data += (Path(__file__).parent / name).read_text(encoding="utf-8").replace(
            "\r\n", "\n"
        ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


async def setup(hass) -> None:
    # Version hash = source-hash prefix + HA version prefix.
    version_hash = await hass.async_add_executor_job(_source_hash)
    version_hash = version_hash[:16] + hashlib.sha256(HA_VERSION.encode()).hexdigest()[:16]

    class FrontendResourceView(HomeAssistantView):
        """Serves the gzipped frontend JS card at /<DOMAIN>/frontend.js."""

        url = f"/{DOMAIN}/frontend.js"
        name = f"{DOMAIN}:frontend"
        requires_auth = False

        async def get(self, request):
            if request.query.get("v") != version_hash:
                return web.Response(status=404)
            return web.Response(
                body=base64.b64decode(FRONTEND),
                content_type="application/javascript",
                headers={
                    "Content-Encoding": "gzip",
                    "Cache-Control": "public, max-age=5184000, immutable",
                },
            )

    class LicenseApiView(HomeAssistantView):
        """API that fetches/returns the DVC license key for a device.

        GET /api/<DOMAIN>/{vp}/{b}?k=<key>
        """

        url = f"/api/{DOMAIN}/{{vp}}/{{b}}"
        name = f"api:{DOMAIN}:frontend"
        requires_auth = True
        cache: dict[str, list] = {}

        async def get(self, request, vp, b) -> web.Response:
            status, body, done = 403, None, False
            key_param = request.query.get("k")
            try:
                supplied_key = (
                    base64.b64decode(key_param).decode().strip().lower() if key_param else ""
                )
            except Exception:
                supplied_key = ""
            key_provided = key_param is not None

            # --- Resolve the device config entry ---
            dev_entry = device_registry.async_get(hass).async_get(b) if b else None
            entry = next(iter(dev_entry.config_entries), None) if dev_entry else None
            if not entry or entry not in hass.data.get(DOMAIN, {}):
                done = True

            if not done:
                device_obj = hass.data[DOMAIN][entry]
                if not (
                    device_obj._device
                    and device_obj._device.status
                    and device_obj._device.status.serial_number
                    and device_obj._device.info
                    and device_obj._device.info.model
                ):
                    status, done = 202, True

            if not done:
                config_entry = hass.config_entries.async_get_entry(entry)
                if not config_entry:
                    status, done = 403, True

            # --- Build device fingerprint token ---
            if not done:
                serial = device_obj._device.status.serial_number
                mac = device_obj._device.info.mac_address or device_obj._device.mac
                model = device_obj._device.info.model

                magic = bytes(
                    x ^ y
                    for x, y in zip(
                        bytes.fromhex("096ea97b63adf781"),
                        bytes.fromhex("7984377e4075ba7a"),
                    )
                ) + bytes(
                    x ^ y
                    for x, y in zip(
                        bytes.fromhex("592cf02330c12db7"),
                        bytes.fromhex("43222f8331d69a23"),
                    )
                )

                a1, a2, a3 = str(serial).encode(), format_mac(str(mac)).encode(), str(model).encode()
                b1 = hashlib.sha256(a1 + magic).digest()
                b2 = hashlib.sha256(a2 + magic[::-1]).digest()
                b3 = hashlib.sha256(a3 + magic).digest()
                c3 = bytes(x ^ y ^ z for x, y, z in zip(b1, b2, b3))
                g = (len(a1) + len(a2) + len(a3)) % 32
                fingerprint = hashlib.sha256(
                    c3[g:] + c3[:g] + b1[::-1] + b2[::-1] + b3[::-1]
                ).hexdigest()

                key = supplied_key if key_provided else (
                    config_entry.options.get(CONF_DVC_KEY) or ""
                ).strip().lower()
                source_hash = None

                # --- Check cache for an already-issued token ---
                cached = self.cache.get(entry)
                if cached and cached[1] == fingerprint and cached[2] == key:
                    if not key_provided or "," in cached[0]:
                        if "," in cached[0]:
                            source_hash = await hass.async_add_executor_job(_source_hash)
                            if cached[0].split(",")[3] == source_hash:
                                status, body, done = 200, cached[0], True
                        else:
                            status, body, done = 200, cached[0], True
                    else:
                        status, done = 400, True

            # --- Build the auth token to present to the license server ---
            if not done:
                fingerprint_bytes = bytes.fromhex(fingerprint)
                dvc_marker = _b64_hex(
                    "NjQ3NjYzMmQ3NDZmNmI2NTZlMmQ3NjMxM2E="
                )  # b"dvc-token-v1:"
                digest = hashlib.sha256(dvc_marker + fingerprint_bytes).digest()
                priv = X25519PrivateKey.from_private_bytes(digest)
                ephemeral_pub = priv.public_key().public_bytes(
                    Encoding.Raw, PublicFormat.Raw
                )
                server_pub = X25519PublicKey.from_public_bytes(SERVER_X25519_PUB)
                shared = priv.exchange(server_pub)
                aes_key = hashlib.sha256(shared).digest()
                ciphertext, tag = AES.new(
                    aes_key, AES.MODE_GCM, nonce=bytes(12)
                ).encrypt_and_digest(fingerprint_bytes)
                auth_token = base64.urlsafe_b64encode(
                    ephemeral_pub + ciphertext + tag
                ).rstrip(b"=").decode()

                # No key yet: if none supplied, hand back the auth token so the
                # frontend can bootstrap key entry.
                if not key:
                    if key_provided:
                        status, done = 400, True
                    else:
                        status, body, done = 200, auth_token, True

            # --- Validate key format; clear invalid stored key ---
            if not done and not KEY_RE.fullmatch(key):
                if key_provided:
                    status, done = 400, True
                else:
                    opts = config_entry.options.copy()
                    if opts.pop(CONF_DVC_KEY, None) is not None:
                        hass.config_entries.async_update_entry(config_entry, options=opts)
                    status, body, done = 200, auth_token, True

            # --- Reject duplicate token per fingerprint across entries ---
            if not done and any(
                k != entry and v[1] == fingerprint for k, v in self.cache.items()
            ):
                if key_provided:
                    status, done = 400, True
                else:
                    status, body, done = 200, auth_token, True

            # --- Fetch / decrypt the license payload ---
            if not done:
                try:
                    if source_hash is None:
                        source_hash = await hass.async_add_executor_job(_source_hash)

                    store = Store(
                        hass,
                        1,
                        USER_DATA_PREFIX
                        + hashlib.sha256(
                            (DVC_MARKER + entry + HA_VERSION).encode()
                        ).hexdigest(),
                    )
                    cached_payload = await store.async_load()
                    payload = None
                    if cached_payload:
                        try:
                            enc_key = hashlib.sha256(f"{key}{fingerprint}{source_hash}".encode()).digest()
                            payload = gzip.decompress(
                                AES.new(
                                    enc_key, AES.MODE_GCM, nonce=bytes.fromhex(cached_payload[0])
                                ).decrypt_and_verify(
                                    base64.b64decode(cached_payload[2]),
                                    bytes.fromhex(cached_payload[1]),
                                )
                            )
                            Ed25519PublicKey.from_public_bytes(LICENSE_ED25519_PUB).verify(
                                bytes.fromhex(cached_payload[3]),
                                fingerprint.encode() + payload,
                            )
                        except Exception:
                            payload = None

                    if payload is None:
                        rejected, update_required = False, False
                        try:
                            async with aiohttp.ClientSession(
                                headers={"User-Agent": f"DreameVacuum/{VERSION}"}
                            ) as session:
                                async with session.post(
                                    f"{LICENSE_SERVER}?v={VERSION}&hv={HA_VERSION}",
                                    data=f"{key},{auth_token},{source_hash},{model}",
                                    timeout=aiohttp.ClientTimeout(total=15),
                                ) as resp:
                                    fields = (
                                        (await resp.text()).split(",")
                                        if resp.status == 200
                                        else None
                                    )
                                    update_required = resp.status == 426
                                    rejected = resp.status == 403
                        except Exception:
                            fields = None
                            LOGGER.warning(
                                "Could not connect to DVC API. Please check your internet "
                                "connection and try again."
                            )

                        if fields is None:
                            if key_provided:
                                status, done = 400, True
                            elif update_required:
                                status, done = 500, True
                                LOGGER.warning(
                                    "Failed to get DVC. Please make sure the integration "
                                    "is up to date."
                                )
                            elif rejected:
                                # Auth pending: stash token so the card can retry.
                                self.cache[entry] = [auth_token, fingerprint, key]
                                status, body, done = 200, auth_token, True
                            else:
                                status, done = 202, True
                        else:
                            try:
                                enc_key = hashlib.sha256(
                                    f"{key}{fingerprint}{source_hash}".encode()
                                ).digest()
                                payload = gzip.decompress(
                                    AES.new(
                                        enc_key, AES.MODE_GCM, nonce=bytes.fromhex(fields[0])
                                    ).decrypt_and_verify(
                                        base64.b64decode(fields[2]),
                                        bytes.fromhex(fields[1]),
                                    )
                                )
                                Ed25519PublicKey.from_public_bytes(
                                    LICENSE_ED25519_PUB
                                ).verify(bytes.fromhex(fields[3]), fingerprint.encode() + payload)
                            except Exception:
                                payload = None

                            if payload is None:
                                if key_provided:
                                    status, done = 400, True
                                else:
                                    status, done = 500, True
                                    LOGGER.warning("Failed to get DVC. Please try again later.")
                            else:
                                await store.async_save(fields + [source_hash])
                                LOGGER.info("DVC loaded successfully.")

                    # --- Return license payload re-encrypted for the frontend ---
                    if not done:
                        if key_provided:
                            hass.config_entries.async_update_entry(
                                config_entry, options={**config_entry.options, CONF_DVC_KEY: key}
                            )
                        enc_key = hashlib.sha256(f"{fingerprint}:enc".encode()).digest()
                        nonce = get_random_bytes(12)
                        ct = ChaCha20.new(key=enc_key, nonce=nonce).encrypt(gzip.compress(payload))
                        result = f"{auth_token},{nonce.hex()},{base64.b64encode(ct).decode()},{source_hash}"
                        self.cache[entry] = [result, fingerprint, key]
                        status, body, done = 200, result, True
                except Exception:
                    status, done = 500, True

            return web.Response(
                status=status,
                text=body,
                content_type="text/plain" if body is not None else None,
                headers={
                    "Cache-Control": (
                        "private, max-age=5184000"
                        if (body and "," in body and not key_provided)
                        else "no-store"
                    )
                },
            )

    if hass.data.get(f"{DOMAIN}_frontend"):
        return
    prefix = f"/{DOMAIN}/frontend.js?v="
    url = f"{prefix}{version_hash}"

    async def register_lovelace_resource() -> None:
        """Register the frontend JS as a Lovelace module resource."""
        try:
            from homeassistant.components.lovelace.const import LOVELACE_DATA, MODE_STORAGE
        except Exception:
            return
        lovelace = hass.data.get(LOVELACE_DATA)
        if not lovelace or getattr(
            lovelace, "resource_mode", getattr(lovelace, "mode", None)
        ) != MODE_STORAGE:
            return
        try:
            resources = lovelace.resources
            if not resources.loaded:
                await resources.async_load()
                resources.loaded = True
            for item in resources.async_items():
                item_url = item.get("url", "")
                if item_url.startswith(prefix) and item_url != url:
                    await resources.async_delete_item(item["id"])
            if not any(item.get("url") == url for item in resources.async_items()):
                await resources.async_create_item({"res_type": "module", "url": url})
        except Exception:
            LOGGER.warning("Could not register the frontend card as a Lovelace resource.")

    await register_lovelace_resource()
    api_view = LicenseApiView()
    hass.http.register_view(api_view)
    hass.http.register_view(FrontendResourceView())
    hass.data[f"{DOMAIN}_frontend"] = True
    hass.data[CACHE_MAP_KEY] = api_view.cache

    async def invalidate_stale_cache() -> None:
        """Remove cached license stores whose source hash changed (or with no key)."""
        try:
            source_hash = await hass.async_add_executor_job(_source_hash)
        except Exception:
            return
        for entry in hass.config_entries.async_entries(DOMAIN):
            key = (entry.options.get(CONF_DVC_KEY) or "").strip().lower()
            valid_key = bool(key) and bool(KEY_RE.fullmatch(key))
            try:
                store = Store(
                    hass,
                    1,
                    USER_DATA_PREFIX
                    + hashlib.sha256(
                        (DVC_MARKER + entry.entry_id + HA_VERSION).encode()
                    ).hexdigest(),
                )
                if not valid_key:
                    await store.async_remove()
                    continue
                cached = await store.async_load()
                if cached and (len(cached) < 5 or cached[4] != source_hash):
                    await store.async_remove()
            except Exception:
                pass

    hass.async_create_task(invalidate_stale_cache())


async def remove(hass, entry) -> None:
    hass.data.get(CACHE_MAP_KEY, {}).pop(entry.entry_id, None)
    await Store(
        hass,
        1,
        USER_DATA_PREFIX
        + hashlib.sha256(
            (DVC_MARKER + entry.entry_id + HA_VERSION).encode()
        ).hexdigest(),
    ).async_remove()

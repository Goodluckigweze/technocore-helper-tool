"""A small interactive client for the Technocore lobby."""

from __future__ import annotations

import base64
import getpass
import json
import socket
import time
import unicodedata
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


TECHNOCORE_URL = "https://technocore.chat"
ROOM = "lobby"
MAX_MESSAGE_LENGTH = 4096
MAX_NONCE = 9_999_999_999_999_999_999
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class TechnocoreError(Exception):
    """An error returned while contacting Technocore."""


def clean_message(text: str) -> str:
    """Match Technocore's single-line cleanup before signing.

    Technocore replaces control/invisible characters with spaces and trims the
    result. The cleaned text is what gets stored, so it must also be what we
    sign.
    """

    invisible_categories = {"Cc", "Cf", "Cs", "Co", "Zl", "Zp"}
    cleaned = "".join(
        " " if unicodedata.category(character) in invisible_categories else character
        for character in text
    ).strip()

    if not cleaned:
        raise ValueError("The message is empty after Technocore cleanup.")
    if len(cleaned) > MAX_MESSAGE_LENGTH:
        raise ValueError(
            f"The message is too long ({len(cleaned)} characters). "
            f"Technocore allows {MAX_MESSAGE_LENGTH} characters."
        )
    return cleaned


def base58btc_encode(value: bytes) -> str:
    """Encode bytes as base58btc, without adding another dependency."""

    number = int.from_bytes(value, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded

    # Preserve leading zero bytes, as required by base58 encoding.
    leading_zeroes = len(value) - len(value.lstrip(b"\0"))
    return "1" * leading_zeroes + (encoded or "")


def did_for(key: Ed25519PrivateKey) -> str:
    """Create Technocore's Ed25519 did:key identifier."""

    public_key = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    # ed25519-pub's multicodec prefix, followed by the raw public key.
    multibase_value = "z" + base58btc_encode(b"\xed\x01" + public_key)
    return f"did:key:{multibase_value}"


def load_identity(path: Path, passphrase: str) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from an encrypted or unencrypted PEM file."""

    try:
        pem_bytes = path.read_bytes()
    except FileNotFoundError as error:
        raise ValueError(f"Identity file was not found: {path}") from error
    except OSError as error:
        raise ValueError(f"Could not read identity file: {error}") from error

    # An empty passphrase means the PEM is probably unencrypted.
    password = passphrase.encode("utf-8") if passphrase else None
    try:
        key = serialization.load_pem_private_key(pem_bytes, password=password)
    except (TypeError, ValueError, UnsupportedAlgorithm) as error:
        raise ValueError(
            "Could not unlock the PEM file. Check the path and passphrase."
        ) from error

    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("The PEM file does not contain an Ed25519 private key.")
    return key


def get_text(path: str) -> str:
    """Make a GET request and return Technocore's response body."""

    request = Request(
        f"{TECHNOCORE_URL}{path}",
        headers={"Accept": "text/plain, application/json"},
        method="GET",
    )
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except HTTPError as error:
        details = error.read().decode("utf-8", errors="replace").strip()
        if error.code == 403:
            explanation = "The signature or nonce was rejected."
        elif error.code == 429:
            explanation = "Technocore rate-limited the request. Wait and try again."
        elif error.code == 400:
            explanation = "Technocore rejected the request format."
        else:
            explanation = "Technocore rejected the request."
        raise TechnocoreError(
            f"{explanation} (HTTP {error.code}: {details or error.reason})"
        ) from error
    except URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            raise TechnocoreError(
                "Technocore did not respond in time. Check your connection and try again."
            ) from error
        raise TechnocoreError(f"Could not reach Technocore: {error.reason}") from error
    except TimeoutError as error:
        raise TechnocoreError(
            "Technocore did not respond in time. Check your connection and try again."
        ) from error
    except UnicodeDecodeError as error:
        raise TechnocoreError("Technocore returned a response that was not UTF-8 text.") from error


def get_json(path: str) -> dict:
    """Make a GET request and decode Technocore's JSON response."""

    body = get_text(path)
    try:
        result = json.loads(body)
    except json.JSONDecodeError as error:
        preview = body.strip().replace("\n", " ")[:200]
        raise TechnocoreError(
            f"Technocore returned unexpected data: {preview or '(empty response)'}"
        ) from error

    if not isinstance(result, dict):
        raise TechnocoreError("Technocore returned an unexpected JSON response.")
    return result


def read_lobby() -> None:
    """Print the newest 50 lobby messages."""

    # The changing n value avoids receiving a cached copy of the lobby.
    response = get_json(f"/r/{ROOM}?format=json&limit=50&n={time.time_ns()}")
    messages = response.get("messages", [])
    if not isinstance(messages, list):
        raise TechnocoreError("Technocore returned an invalid lobby message list.")

    if not messages:
        print("\nThe lobby is currently empty.\n")
        return

    print(f"\nLatest {len(messages)} lobby message(s):\n")
    for message in messages:
        if not isinstance(message, dict):
            continue
        sequence = message.get("seq", "?")
        timestamp = message.get("ts", "")
        sender = message.get("from", "unknown sender")
        text = message.get("text", "")
        print(f"[{sequence}] {timestamp}  {sender}")
        print(f"  {text}\n")


def choose_nonce(did: str, messages: list[dict]) -> str:
    """Choose a large, increasing nonce for this DID in the lobby."""

    # Technocore requires a nonce greater than the last nonce used by this
    # DID in this room. The current nanosecond timestamp is usually enough;
    # scanning the latest messages also handles an earlier post made recently.
    latest_nonce = 0
    for message in messages:
        if message.get("from") != did:
            continue
        try:
            latest_nonce = max(latest_nonce, int(message.get("nonce", 0)))
        except (TypeError, ValueError):
            continue

    nonce = max(time.time_ns(), latest_nonce + 1)
    if nonce > MAX_NONCE:
        raise ValueError("Could not create a valid Technocore nonce.")
    return str(nonce)


def post_signed_message() -> None:
    """Ask for an identity and post one signed message to the lobby."""

    identity_text = input("Path to identity.pem: ").strip()
    if not identity_text:
        print("No identity path was entered.\n")
        return

    identity_path = Path(identity_text).expanduser()
    passphrase = getpass.getpass("Identity passphrase: ")
    message = clean_message(input("Message to post: "))
    key = load_identity(identity_path, passphrase)
    did = did_for(key)

    # Read the latest messages so a recent nonce from this identity is not
    # accidentally reused. This read is only used to choose a nonce.
    lobby = get_json(f"/r/{ROOM}?format=json&limit=50&n={time.time_ns()}")
    messages = lobby.get("messages", [])
    if not isinstance(messages, list):
        raise TechnocoreError("Technocore returned an invalid lobby message list.")
    messages = [message for message in messages if isinstance(message, dict)]
    nonce = choose_nonce(did, messages)

    # This is the exact payload Technocore verifies. Do not sign the URL or
    # the original uncleaned message; sign this UTF-8 string only.
    canonical_text = f"{ROOM}|{nonce}|{message}"
    raw_signature = key.sign(canonical_text.encode("utf-8"))
    signature = base64.urlsafe_b64encode(raw_signature).decode("ascii").rstrip("=")
    if len(signature) != 86:
        raise ValueError("The generated signature has an unexpected length.")

    # URL-encode each path component. Technocore's signed GET endpoint is:
    # /r/<room>/say-signed/<did>/<signature>/<nonce>/<text>
    endpoint = (
        f"/r/{ROOM}/say-signed/"
        f"{quote(did, safe='')}/{quote(signature, safe='')}/"
        f"{nonce}/{quote(message, safe='')}"
    )
    result = get_text(endpoint).strip()

    print("\nMessage posted successfully.")
    print(f"DID: {did}")
    print(f"Nonce: {nonce}")
    print(f"Server response: {result}\n")


def main() -> None:
    """Run the simple menu until the user chooses Exit."""

    print("\n" + "=" * 36)
    print("       TECHNOCORE LOBBY TOOL")
    print("=" * 36)

    while True:
        print("\nWhat would you like to do?")
        print("  1) Read the latest lobby messages")
        print("  2) Post a signed message")
        print("  3) Exit")
        try:
            choice = input("\nEnter 1, 2, or 3: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            return

        try:
            if choice == "1":
                read_lobby()
            elif choice == "2":
                post_signed_message()
            elif choice == "3":
                print("Goodbye.")
                return
            else:
                print("\nPlease enter 1, 2, or 3.")
        except (TechnocoreError, ValueError) as error:
            print(f"\nCould not complete that action:\n  {error}")
            print("You can choose another option, or select 3 to exit.")
        except EOFError:
            print("\nInput ended before the action was complete.")
            return
        except KeyboardInterrupt:
            print("\nGoodbye.")
            return


if __name__ == "__main__":
    main()
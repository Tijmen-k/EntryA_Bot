"""
One-off helper to generate the dashboard login credential.

Run locally:  python scripts/generate_login_secret.py

Prompts for a password, derives a salted PBKDF2 hash of it, and prints two
lines to paste into .env. The plaintext password is never written to disk or
stored anywhere — only the salt and hash are kept, and the hash cannot be
reversed back into the password.

The iteration count below must match _PBKDF2_ITERATIONS in app.py.
"""
import getpass
import hashlib
import secrets

_PBKDF2_ITERATIONS = 200_000


def main() -> None:
    password = getpass.getpass("New dashboard password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords did not match — aborted.")
        return
    if not password:
        print("Password cannot be empty — aborted.")
        return

    salt = secrets.token_hex(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    ).hex()

    print("\nAdd these lines to your .env file:\n")
    print(f"DASHBOARD_PASSWORD_SALT={salt}")
    print(f"DASHBOARD_PASSWORD_HASH={password_hash}")


if __name__ == "__main__":
    main()

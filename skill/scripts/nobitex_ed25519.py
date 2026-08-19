"""Ed25519 signing for Nobitex API keys.

Nobitex signs requests with Ed25519, not HMAC. `cryptography` or PyNaCl is used when
installed; otherwise a pure-Python RFC 8032 implementation runs so the skill works on
a bare interpreter. The fallback is slow (~10ms/signature) but request rates here are
a handful per minute, so it never matters.

The pure-Python path is verified against RFC 8032 test vectors in self_test().
"""

import base64
import hashlib

__all__ = ["sign_b64", "decode_secret", "backend_name", "self_test"]


# --------------------------------------------------------------------------------
# Pure-Python reference implementation (RFC 8032, Ed25519)
# --------------------------------------------------------------------------------

_Q = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493


def _inv(x):
    return pow(x, _Q - 2, _Q)


_D = -121665 * _inv(121666) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y):
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if x % 2 != 0:
        x = _Q - x
    return x


_BY = 4 * _inv(5) % _Q
_BX = _xrecover(_BY)
_B = (_BX % _Q, _BY % _Q, 1, (_BX * _BY) % _Q)


def _add(p, q):
    """Unified twisted-Edwards addition in extended coordinates (a = -1).

    Unified means the same formula is correct for doubling, which keeps the
    scalar-multiplication loop to a single case.
    """
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _Q
    b = (y1 + x1) * (y2 + x2) % _Q
    c = t1 * 2 * _D * t2 % _Q
    dd = z1 * 2 * z2 % _Q
    e, f, g, h = b - a, dd - c, dd + c, b + a
    return (e * f % _Q, g * h % _Q, f * g % _Q, e * h % _Q)


def _scalarmult(p, e):
    q = (0, 1, 1, 0)
    while e > 0:
        if e & 1:
            q = _add(q, p)
        p = _add(p, p)
        e >>= 1
    return q


def _encodepoint(p):
    x, y, z, _t = p
    zi = _inv(z)
    x = x * zi % _Q
    y = y * zi % _Q
    bits = [(y >> i) & 1 for i in range(255)] + [x & 1]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(32))


def _h(m):
    return hashlib.sha512(m).digest()


def _pure_sign(seed, message):
    hd = _h(seed)
    a = 2 ** 254 + sum(2 ** i * ((hd[i // 8] >> (i % 8)) & 1) for i in range(3, 254))
    pub = _encodepoint(_scalarmult(_B, a))
    r = int.from_bytes(_h(hd[32:] + message), "little") % _L
    rr = _encodepoint(_scalarmult(_B, r))
    k = int.from_bytes(_h(rr + pub + message), "little") % _L
    s = (r + k * a) % _L
    return rr + s.to_bytes(32, "little")


# --------------------------------------------------------------------------------
# Backend selection
# --------------------------------------------------------------------------------

_BACKEND = "pure-python"
_impl_sign = _pure_sign

try:  # preferred: widely installed, constant-time
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    def _crypto_sign(seed, message):
        return Ed25519PrivateKey.from_private_bytes(seed).sign(message)

    _impl_sign = _crypto_sign
    _BACKEND = "cryptography"
except ImportError:
    try:
        from nacl.signing import SigningKey

        def _nacl_sign(seed, message):
            return SigningKey(seed).sign(message).signature

        _impl_sign = _nacl_sign
        _BACKEND = "pynacl"
    except ImportError:
        pass


def backend_name():
    return _BACKEND


# --------------------------------------------------------------------------------
# Public helpers
# --------------------------------------------------------------------------------


def decode_secret(secret):
    """Normalise a Nobitex privateKey into a 32-byte Ed25519 seed.

    Nobitex returns it base64url-encoded. Some tooling stores it as standard base64,
    as hex, or as a 64-byte seed||public blob, so all of those are accepted -
    a credential that fails to parse is a confusing failure mode at 3am.
    """
    if isinstance(secret, bytes):
        raw = secret
    else:
        s = secret.strip().strip('"').strip("'")
        raw = None
        try:
            raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        except Exception:
            pass
        if raw is None or len(raw) not in (32, 64):
            try:
                raw = base64.b64decode(s + "=" * (-len(s) % 4))
            except Exception:
                raw = None
        if (raw is None or len(raw) not in (32, 64)) and len(s) in (64, 128):
            try:
                raw = bytes.fromhex(s)
            except ValueError:
                raw = None
    if raw is None or len(raw) not in (32, 64):
        raise ValueError(
            "Could not decode the API secret into a 32-byte Ed25519 seed. Expected "
            "the base64 privateKey shown once when the key was created."
        )
    return raw[:32]


def sign_b64(secret, timestamp, method, url, body="", urlsafe=False):
    """Return base64(Ed25519(timestamp + method + url + body)).

    `url` must be the request path including the query string, e.g.
    "/market/orders/list?fromId=123" - signing the path without the query is the
    single most common cause of 401s here.
    """
    seed = decode_secret(secret)
    payload = f"{timestamp}{method.upper()}{url}{body}".encode("utf-8")
    sig = _impl_sign(seed, payload)
    enc = base64.urlsafe_b64encode if urlsafe else base64.b64encode
    return enc(sig).decode("ascii")


def self_test():
    """Check the pure-Python signer against RFC 8032 vectors and any live backend."""
    vectors = [
        # (seed hex, message hex, expected signature hex)
        ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
         "",
         "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc6"
         "1e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
        ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
         "72",
         "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458"
         "f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
        ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
         "af82",
         "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae6"
         "7f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    ]
    failures = []
    for seed_hex, msg_hex, expected in vectors:
        got = _pure_sign(bytes.fromhex(seed_hex), bytes.fromhex(msg_hex)).hex()
        if got != expected:
            failures.append(f"pure-python vector mismatch for seed {seed_hex[:8]}...")
        if _BACKEND != "pure-python":
            got2 = _impl_sign(bytes.fromhex(seed_hex), bytes.fromhex(msg_hex)).hex()
            if got2 != expected:
                failures.append(f"{_BACKEND} vector mismatch for seed {seed_hex[:8]}...")
    return failures


if __name__ == "__main__":
    import sys
    fails = self_test()
    print(f"backend: {backend_name()}")
    if fails:
        for f in fails:
            print("FAIL:", f)
        sys.exit(1)
    print("Ed25519 self-test passed (RFC 8032 vectors).")

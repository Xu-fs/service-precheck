"""JWT固定算法与用途；不接受令牌自报的算法，不在载荷中保存个人材料。"""
import time
from uuid import uuid4
import jwt
from zhengwu_precheck.core.errors import AppError

ISSUER, AUDIENCE = "zhengwu-precheck", "zhengwu-staff"


class StaffJWT:
    def __init__(self, secret):
        self.secret = secret

    def ensure_ready(self):
        if not self.secret or len(self.secret.encode()) < 32:
            raise AppError(503, "STAFF_AUTH_UNCONFIGURED", "工作人员登录尚未配置，请联系维护人员设置独立签名密钥")

    def issue(self, user_id, session_id, version, rotation, expiry, jti=None):
        self.ensure_ready()
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": AUDIENCE, "sub": user_id, "sid": session_id, "jti": jti or str(uuid4()),
                  "ver": version, "rot": rotation, "iat": now, "nbf": now, "exp": min(now + 900, int(expiry))}
        return jwt.encode(claims, self.secret, algorithm="HS256")

    def decode(self, token, *, allow_expired=False):
        self.ensure_ready()
        try:
            claims = jwt.decode(token, self.secret, algorithms=["HS256"], issuer=ISSUER, audience=AUDIENCE,
                                options={"require": ["iss", "aud", "sub", "sid", "jti", "ver", "rot", "iat", "nbf", "exp"], "strict_aud": True, "verify_exp": not allow_expired})
            if any(not isinstance(claims[key], str) or not claims[key] for key in ("sub", "sid", "jti")):
                raise ValueError()
            if any(type(claims[key]) is not int for key in ("ver", "rot", "iat", "nbf", "exp")):
                raise ValueError()
            return claims
        except (jwt.PyJWTError, ValueError, TypeError):
            raise AppError(401, "STAFF_AUTH_EXPIRED", "工作人员登录已失效，请重新登录") from None

"""JWT authentication utilities for ZilloAgent."""
import os
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from database import Agent, get_db

SECRET_KEY = os.getenv("SECRET_KEY", "zilloagent-jwt-secret-change-me")
ALGORITHM  = "HS256"
TOKEN_DAYS = 30

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
bearer      = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_token(agent_id: int) -> str:
    expire  = datetime.utcnow() + timedelta(days=TOKEN_DAYS)
    payload = {"sub": str(agent_id), "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def get_current_agent(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer),
    db: Session = Depends(get_db),
) -> Agent:
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload   = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        agent_id  = int(payload["sub"])
    except (JWTError, ValueError, KeyError):
        raise HTTPException(status_code=401, detail="Invalid token")
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=401, detail="Agent not found")
    return agent

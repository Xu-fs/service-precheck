"""请求字段严格限定，不接受客户端传入所有者ID。"""
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: UUID | None = None
    question: str = Field(min_length=1, max_length=2000)

    @field_validator("question", mode="before")
    @classmethod
    def strip_question(cls, value):
        return value.strip() if isinstance(value, str) else value


class MatterInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    matter_id: str = Field(max_length=100)
    scenario_id: str = Field(max_length=100)
    confirmation_token: str = Field(min_length=32, max_length=128)


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)

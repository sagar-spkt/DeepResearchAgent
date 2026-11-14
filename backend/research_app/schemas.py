from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class ConversationInput(Input):
    title: str = Field("New research", min_length=1, max_length=200)


class RunInput(Input):
    prompt: str = Field(min_length=1, max_length=20000)

    @field_validator("prompt")
    @classmethod
    def input_budget(cls, value):
        if len(value.encode("utf-8")) > 6000:
            raise ValueError(
                "Research prompt exceeds the 6000-byte UTF-8 input budget; upload long source material as a document"
            )
        return value


class MemoryInput(Input):
    content: str = Field(min_length=1, max_length=4000)


class UserUpdate(Input):
    approved: bool | None = None
    role: Literal["user", "admin", "owner"] | None = None


class IdentityInput(Input):
    id_token: str = Field(min_length=1, max_length=16384, repr=False)

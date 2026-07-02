from models.schemas.base import InputSchemaBase, SchemaBase
from pydantic import Field, constr

AccountId = constr(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
ShortText = constr(min_length=1, max_length=120, strip_whitespace=True)
NormalText = constr(max_length=2000, strip_whitespace=True)
InstructionText = constr(max_length=12000, strip_whitespace=True)


class AccountSummary(SchemaBase):
    id: AccountId
    name: ShortText
    description: NormalText = ""
    instructions: InstructionText = ""


class AccountDetail(AccountSummary):
    pass


class AccountCreate(InputSchemaBase):
    id: AccountId
    name: ShortText
    description: NormalText = ""
    instructions: InstructionText = Field(
        default="",
        description=(
            "Account-level persistent behavior instructions injected into the agent prompt."
        ),
    )


class AccountUpdate(InputSchemaBase):
    name: ShortText | None = None
    description: NormalText | None = None
    instructions: InstructionText | None = None

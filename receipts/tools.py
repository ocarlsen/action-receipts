"""The tools the agent may call. Every one is strict: the model's input must match the schema."""


def _tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False,
        },
    }


_ID = {"type": "string"}
_CENTS = {"type": "integer", "description": "Amount in cents, e.g. 41250 for $412.50."}

TOOLS = [
    _tool("verify_member",
          "Verify the caller's identity. Required before any account information or action. "
          "Ask the member for their membership number and the last four digits of their phone.",
          {"membership_no": _ID, "phone_last4": _ID}),
    _tool("list_memberships",
          "List the verified member's memberships with their accounts, cards and loans. "
          "A member may hold more than one membership; ask which one when it matters.",
          {}),
    _tool("get_balance", "Current balance of one of the member's accounts.", {"account_id": _ID}),
    _tool("get_transactions", "Recent transactions on one of the member's accounts, newest first.",
          {"account_id": _ID, "limit": {"type": "integer", "description": "1 to 20"}}),
    _tool("freeze_card", "Freeze one of the member's cards, e.g. when it is lost or stolen.",
          {"card_id": _ID}),
    _tool("pay_loan", "Pay toward one of the member's loans from one of their accounts.",
          {"loan_id": _ID, "from_account_id": _ID, "amount_cents": _CENTS}),
    _tool("transfer", "Move money between two of the member's own accounts.",
          {"from_account_id": _ID, "to_account_id": _ID, "amount_cents": _CENTS}),
    _tool("update_address", "Change the verified member's mailing address.",
          {"address": {"type": "string", "description": "The full new address."}}),
    _tool("handoff",
          "Hand the conversation to a human member-service representative. Use it when the member "
          "asks for a person, or when you cannot complete the request with the other tools. "
          "The summary travels with the member, so they never have to repeat themselves.",
          {"reason": {"type": "string"},
           "summary": {"type": "string", "description": "What the member wants and what has been done."}}),
]

READS = {"verify_member", "list_memberships", "get_balance", "get_transactions"}
WRITES = {"freeze_card", "pay_loan", "transfer", "update_address"}

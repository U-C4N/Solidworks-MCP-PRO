"""Public contracts for the v0.6 engineering and presentation workflows."""
from mcp import types


def release_tools() -> list[types.Tool]:
    from .design_validation import design_tools
    from .variant_study import variant_tools
    from .shot_pack import shot_tools
    return [types.Tool(
        name="pro_connection_doctor",
        description=("Read-only identity and tool-profile report from this running PRO server. "
                     "Optionally inspect conventional client settings without executing commands or exposing secrets. "
                     "Does not attach to CAD or establish render availability."),
        inputSchema={"type": "object", "properties": {
            "include_client_configs": {"type": "boolean", "default": False},
        }, "additionalProperties": False},
        annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                          idempotentHint=True, openWorldHint=False),
    )] + design_tools() + variant_tools() + shot_tools()

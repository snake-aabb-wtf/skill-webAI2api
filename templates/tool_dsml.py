import json
import re
from typing import Optional


DSML_TAG_OPEN = "<|DSML|"
DSML_TAG_CLOSE = "|>"

DSML_PROMPT_TEMPLATE = """You have access to the following tools. When you need to use a tool, your response MUST follow the DSML (DeepSeek Markup Language) format exactly.

Available tool definitions:
{tool_definitions}

When you decide to call a tool, output ONLY the DSML block with no additional text:
<|DSML|tool_calls>
  <|DSML|invoke name="TOOL_NAME">
    <|DSML|parameter name="PARAM_NAME"><![CDATA[PARAM_VALUE]]></|DSML|parameter>
  </|DSML|invoke>
</|DSML|tool_calls>

Rules:
1. If you don't need to use any tool, respond with normal text — do NOT output DSML.
2. Each <|DSML|invoke> block calls exactly one tool. For multiple parallel calls, put multiple <|DSML|invoke> blocks inside <|DSML|tool_calls>.
3. Parameter values MUST be wrapped in <![CDATA[...]]>.
4. You may combine normal text AND a DSML block in a single response if appropriate."""

DSML_REQUIRED_PROMPT = """You MUST use one of the following tools in your response. Do NOT respond with normal text — output a DSML block.

Available tool definitions:
{tool_definitions}

Output format:
<|DSML|tool_calls>
  <|DSML|invoke name="TOOL_NAME">
    <|DSML|parameter name="PARAM_NAME"><![CDATA[PARAM_VALUE]]></|DSML|parameter>
  </|DSML|invoke>
</|DSML|tool_calls>"""


def _format_function_def(name: str, desc: str, params: dict) -> str:
    """Format a single tool definition for the DSML prompt."""
    lines = [f"- {name}: {desc}"]
    if params and "properties" in params:
        props = params["properties"]
        required = set(params.get("required", []))
        for pname, pinfo in props.items():
            req = " (required)" if pname in required else ""
            ptype = pinfo.get("type", "string")
            pdesc = pinfo.get("description", "")
            lines.append(f"    - {pname}: {ptype}{req} — {pdesc}")
    return "\n".join(lines)


def build_dsml_tool_prompt(tools: list, tool_choice: Optional[str] = None) -> str:
    """Build a DSML system prompt from OpenAI-format tools list.

    tools: OpenAI format [{type: "function", function: {name, description, parameters}}]
    tool_choice: "auto", "none", "required", or None
    """
    if not tools:
        return ""

    defs = []
    for t in tools:
        if t.get("type") == "function":
            fn = t.get("function", {})
            defs.append(_format_function_def(
                fn.get("name", "unknown"),
                fn.get("description", ""),
                fn.get("parameters", {}),
            ))

    tool_block = "\n\n".join(defs)

    if tool_choice == "required":
        return DSML_REQUIRED_PROMPT.format(tool_definitions=tool_block)
    return DSML_PROMPT_TEMPLATE.format(tool_definitions=tool_block)


def has_dsml_content(text: str) -> bool:
    """Quick check if text contains DSML tags."""
    return DSML_TAG_OPEN in text


def parse_dsml_invoke(dsml_text: str) -> list[dict]:
    """Parse DSML XML text into OpenAI-format tool_calls.

    Returns list of tool_call dicts with id, type, function.{name, arguments}
    """
    tool_calls = []
    id_counter = 0

    # Match <|DSML|invoke name="..."> ... </|DSML|invoke>
    invoke_pattern = re.compile(
        re.escape(DSML_TAG_OPEN) + r'invoke\s+name="([^"]+)"' + re.escape(DSML_TAG_CLOSE)
        + r'(.*?)'
        + re.escape(DSML_TAG_OPEN) + r'/invoke' + re.escape(DSML_TAG_CLOSE),
        re.DOTALL,
    )

    for match in invoke_pattern.finditer(dsml_text):
        name = match.group(1)
        body = match.group(2).strip()

        # Extract parameters
        param_pattern = re.compile(
            re.escape(DSML_TAG_OPEN) + r'parameter\s+name="([^"]+)"' + re.escape(DSML_TAG_CLOSE)
            + r'<!\[CDATA\[(.*?)\]\]>'
            + re.escape(DSML_TAG_OPEN) + r'/parameter' + re.escape(DSML_TAG_CLOSE),
            re.DOTALL,
        )

        arguments = {}
        for pmatch in param_pattern.finditer(body):
            arguments[pmatch.group(1)] = pmatch.group(2)

        id_counter += 1
        tool_calls.append({
            "id": f"call_dsml_{id_counter}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        })

    return tool_calls


def strip_dsml_tags(text: str) -> str:
    """Remove all DSML tags from text, leaving only non-DSML content."""
    if not has_dsml_content(text):
        return text

    result = []
    pos = 0
    while pos < len(text):
        open_idx = text.find(DSML_TAG_OPEN, pos)
        if open_idx == -1:
            result.append(text[pos:])
            break
        if open_idx > pos:
            result.append(text[pos:open_idx])
        close_idx = text.find(DSML_TAG_CLOSE, open_idx)
        if close_idx == -1:
            break
        pos = close_idx + len(DSML_TAG_CLOSE)

    return "".join(result).strip()

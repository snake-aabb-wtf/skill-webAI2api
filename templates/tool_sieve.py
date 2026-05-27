from enum import Enum, auto
from typing import Optional
from tool_dsml import (
    has_dsml_content,
    parse_dsml_invoke,
    DSML_TAG_OPEN,
    DSML_TAG_CLOSE,
)


class SieveState(Enum):
    NORMAL = auto()
    CAPTURING = auto()


class FeedResult:
    def __init__(self):
        self.text_parts: list[str] = []
        self.tool_calls: list[dict] = []
        self.pending = False


class StreamSieve:
    """Real-time stream sieve that separates normal text from DSML tool call tags.

    Processes SSE content chunk by chunk, character by character.
    When a DSML opening tag is detected, switches to capture mode
    and accumulates content until the closing tag, then parses the
    entire DSML block.
    """

    def __init__(self):
        self._state = SieveState.NORMAL
        self._buffer = ""
        self._text_buffer = ""
        self._in_dsml_tag = False
        self._tag_depth = 0

    def feed(self, chunk: str) -> FeedResult:
        """Process a chunk of text, return separated text parts and tool calls.

        chunk: incoming text fragment (may be partial SSE content)
        """
        result = FeedResult()

        for ch in chunk:
            if self._state == SieveState.NORMAL:
                if ch == DSML_TAG_OPEN[0]:
                    # Possible start of DSML tag — begin watching
                    self._buffer = ch
                    self._state = SieveState.CAPTURING
                    self._in_dsml_tag = True
                    self._tag_depth = 1
                    # Flush accumulated normal text before capture
                    if self._text_buffer:
                        result.text_parts.append(self._text_buffer)
                        self._text_buffer = ""
                else:
                    self._text_buffer += ch

            else:
                if self._state == SieveState.CAPTURING:
                    self._buffer += ch

                    # Track tag depth for proper nesting
                    if DSML_TAG_OPEN in self._buffer[-len(DSML_TAG_OPEN):]:
                        self._tag_depth += 1
                        self._in_dsml_tag = True

                    if DSML_TAG_CLOSE in self._buffer[-len(DSML_TAG_CLOSE):]:
                        self._tag_depth -= 1

                    # Check if we've closed all tags
                    if self._tag_depth <= 0:
                        # Verify this was actually DSML content
                        if has_dsml_content(self._buffer):
                            tool_calls = parse_dsml_invoke(self._buffer)
                            result.tool_calls.extend(tool_calls)
                        else:
                            self._text_buffer = self._buffer + self._text_buffer

                        self._buffer = ""
                        self._state = SieveState.NORMAL
                        self._in_dsml_tag = False
                        self._tag_depth = 0

        # Check if we're mid-capture
        if self._state == SieveState.CAPTURING:
            result.pending = True

        # Flush any remaining normal text
        if self._text_buffer:
            result.text_parts.append(self._text_buffer)
            self._text_buffer = ""

        return result

    def flush(self) -> FeedResult:
        """Called when stream ends. Handles unclosed DSML tags gracefully.

        If a DSML capture was in progress and never closed:
        - Treat the captured content as normal text
        - No tool calls are generated
        """
        result = FeedResult()

        # Flush any text left in buffer
        if self._text_buffer:
            result.text_parts.append(self._text_buffer)
            self._text_buffer = ""

        # If mid-capture, the buffer was probably not DSML — return as text
        if self._buffer and self._state == SieveState.CAPTURING:
            result.text_parts.append(self._buffer)
            self._buffer = ""

        self._state = SieveState.NORMAL
        return result

    def reset(self):
        """Reset sieve to initial state."""
        self._state = SieveState.NORMAL
        self._buffer = ""
        self._text_buffer = ""
        self._in_dsml_tag = False
        self._tag_depth = 0

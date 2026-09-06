"""
telephony.py - Twilio voice webhook glue.

Deliberately thin: this module's only job is (1) speak text via TwiML and
(2) collect the next spoken instruction via <Gather input="speech">, using
Twilio's own speech-to-text so no separate STT service is required to get
a working demo. It never decides whether a request is safe - that is
agents/live_action_guard.py's job - so the guard logic stays testable
without Twilio at all (see tests note in the accompanying writeup).

VOICE QUALITY: Twilio's built-in <Say> uses Amazon Polly voices, which
are intelligible but noticeably robotic. If voice quality matters for the
demo, swap <Say> for a pre-synthesized ElevenLabs clip played via <Play>:
generate the audio server-side (ElevenLabs API), save it to
static/audio/<hash>.mp3, and <Play> that URL instead. That is a ~20 line
change isolated to `say()` below - nothing else in this file or in
live_action_guard.py needs to change. Not implemented here because it
needs a live ElevenLabs API key to test at all.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

VOICE = "Polly.Joanna"  # Twilio built-in TTS voice; swap in say() for ElevenLabs if needed


def say(text: str) -> str:
    return f'<Say voice="{VOICE}">{escape(text)}</Say>'


def gather_next_instruction(gather_action_url: str, prompt: str | None = None) -> str:
    """TwiML for: optionally say `prompt`, then listen for the caller's
    next spoken instruction and POST the transcript to gather_action_url.
    speechTimeout="auto" lets Twilio decide when the caller has stopped
    talking, which is what makes this usable for arbitrary/unscripted
    caller phrasing rather than a fixed-length recording window."""
    prompt_tag = say(prompt) if prompt else ""
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<Response>"
        f"{prompt_tag}"
        f'<Gather input="speech" action="{escape(gather_action_url)}" method="POST" '
        f'speechTimeout="auto" language="en-US">'
        "</Gather>"
        # If the caller says nothing, loop back rather than hanging up -
        # avoids a dead-air ending mid-demo.
        f'<Redirect method="POST">{escape(gather_action_url)}</Redirect>'
        "</Response>"
    )


def respond_and_continue_listening(spoken_reply: str, gather_action_url: str) -> str:
    """TwiML for: speak the agent's reply to what the caller just asked
    for, then immediately go back to listening for the next instruction -
    this is what makes multiple live requests in one call possible (the
    'judges may ask me to repeat it with a different value live'
    requirement)."""
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<Response>"
        f"{say(spoken_reply)}"
        f'<Gather input="speech" action="{escape(gather_action_url)}" method="POST" '
        f'speechTimeout="auto" language="en-US">'
        "</Gather>"
        f'<Redirect method="POST">{escape(gather_action_url)}</Redirect>'
        "</Response>"
    )


def hangup(spoken_reply: str | None = None) -> str:
    body = say(spoken_reply) if spoken_reply else ""
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}<Hangup/></Response>'

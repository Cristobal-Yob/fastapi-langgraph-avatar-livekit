"""Email is a delivery of the existing business result, never another route."""
import hashlib
import hmac
import os
import re
import time
import unicodedata

from cloud.mail_config import single_address

BASE_RESULT_KEYS = ("route", "reason", "response", "provider", "backend", "model_used", "fallback_used")


def normalized(value):
    return "".join(c for c in unicodedata.normalize("NFKD", value.lower()) if not unicodedata.combining(c))


def addresses(message):
    candidates = re.findall(r"[^\s<>\"',;:]+@[^\s<>\"',;:]+", message)
    valid = []
    for value in candidates:
        try:
            valid.append(single_address(value.rstrip(".)!?")))
        except ValueError:
            return []
    return valid


def literal_greeting_body(message):
    """Only the supported 'mail con un hola [no más]' grammar means literal Hola."""
    text = re.sub(r"\S+@\S+", "", normalized(message))
    text = " ".join(text.split())
    pattern = (r"(?:(?:envia|manda|redacta|prepara|escribe)\s+(?:un\s+)?(?:mail|correo|email)\s+(?:a\s+)?)?"
               r"con\s+un\s+hola(?:\s+(?:no\s+mas|nomas))?(?:\s+a)?[.! ]*")
    return "Hola" if re.fullmatch(pattern, text) else None


def send_restricted(message):
    """Latest human restrictions override both model flags and stored authority."""
    context = re.sub(r"\S+@\S+", "", normalized(message))
    if literal_greeting_body(message):
        # Exempt this exact 'just hello' modifier, never other occurrences of no.
        context = re.sub(r"(\bcon\s+un\s+hola)\s+(?:no\s+mas|nomas)\b", r"\1", context)
    if any(c in message for c in ('"', "'", "`", "“", "”", "«", "»", "?", "¿")):
        return True
    return bool(re.search(
        r"\b(no|nunca|jamas|sin|not|never|ejemplo|example|simula|supongamos|"
        r"borrador\w*|draft\w*|cancel\w*|olvid\w*|"
        r"si|cuando|hasta|despues|luego|antes|confirm\w*|esper\w*|"
        r"if|unless|when|until|later|after|before|would|could)\b", context))


def literal_intent(message):
    """Conservative supported command forms. Model metadata alone grants nothing."""
    text = normalized(message).strip()
    if any(c in message for c in ('"', "'", "`", "“", "”", "«", "»")):
        return None
    if not re.search(r"\b(correo|email|e-mail|mail)\b", text) and "@" not in text:
        return None
    text = re.sub(r"^por favor[, ]+|^please[, ]+", "", text)
    draft = re.match(r"^(redacta(?:r|me)?|prepara(?:r|me)?|escrib[ei](?:r|me)?|draft|write)\b", text)
    if draft:
        return "draft"
    if send_restricted(message):
        return None
    direct = r"(?:envia(?:r|me|le|les|lo|la)?|envie(?:s|n)?|manda(?:r|me|le|les|lo|la)?|mande(?:s|n)?|send|email)"
    if re.match(rf"^{direct}\b", text) or re.match(
        rf"^(?:quiero|necesito|puedes|podes|podrias)\s+(?:que\s+)?{direct}\b", text
    ):
        return "send"
    return None


def meaningful_content(message):
    text = normalized(message)
    text = re.sub(r"\S+@\S+", "", text)
    ignored = set("informacion info correo correos mail email enviar envio envia envie envies envien enviame enviale "
                  "enviarle mandar manda mandame mande mandes send please favor por un una a al para de del el la "
                  "los las y con que quiero necesito puedes podes podrias me mi mis su sus sobre asunto tema "
                  "esto eso hola an the to via redacta redactar prepara preparar escribe escribir borrador draft write "
                  "ahora porfa porfavor gracias urgente pronto hoy manana inmediatamente mensaje texto contenido "
                  "algo cosa siguiente necesario necesaria now today about thanks thank you urgently".split())
    command = r"(?:envia(?:r|me|le|les|lo|la)?|envie(?:s|n)?|manda(?:r|me|le|les|lo|la)?|mande(?:s|n)?)"
    return any(len(word) >= 3 and word not in ignored and not re.fullmatch(command, word)
               for word in re.findall(r"[a-z]+", text))


def pending_reply(pending, message):
    if not pending:
        return None
    text = normalized(message).strip().rstrip(".! ")
    if re.fullmatch(r"(?:cancela(?:r)?|cancelar correo|cancelar el correo|olvida(?:lo)?|no envies(?: el correo)?|cancel)", text):
        return "cancel"
    if send_restricted(message):
        return "restricted"
    if pending["needs"] == "recipient":
        candidate = re.sub(r"^(?:mi correo es|mi email es|correo:|email:|a)\s*", "", message.strip(), flags=re.I)
        try:
            return {"recipient": single_address(candidate)}
        except ValueError:
            return None
    body = literal_greeting_body(message)
    if body and re.match(r"^con\s+un\s+hola\b", text) and not addresses(message):
        return {"content": True, "literal_body": body}
    if re.match(r"^(sobre |informacion de |tema: )", text) and meaningful_content(text):
        return {"content": True}
    return None


def initial_plan(state):
    mode = literal_intent(state["message"])
    decision = state.get("delivery", "chat")
    if mode is None or decision not in {"send", "draft"}:
        return None
    if mode == "send" and decision != "send":
        return None
    recipients = addresses(state["message"])
    recipient = recipients[0] if len(recipients) == 1 else None
    semantic = state.get("email_recipient")
    if semantic and recipient != semantic:
        recipient = None
    order_digest = hashlib.sha256(state["message"].encode("utf-8")).hexdigest()
    identity = state.get("request_id") or order_digest
    action_key = hashlib.sha256(
        f"chat-email-order/v1\0{state.get('conversation_id', '')}\0{identity}".encode()).hexdigest()
    now = state.get("email_now", time.time())
    body = literal_greeting_body(state["message"])
    return {"mode": mode, "recipient": recipient, "content": bool(body) or meaningful_content(state["message"]),
            "literal_body": body,
            "action_key": action_key, "order_digest": order_digest,
            "issued_at": now, "expires_at": now + 300}

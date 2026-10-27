from typing import Literal, NotRequired, Protocol, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from app.prompts import ROUTER_PROMPT, RESPONSE_PROMPT
from app.email_delivery import BASE_RESULT_KEYS, initial_plan, pending_reply

Route = Literal["preventa", "postventa", "generica"]


class RouteDecision(BaseModel):
    route: Route
    reason: str = Field(min_length=1, max_length=500)
    delivery: Literal["chat", "draft", "send"] = "chat"
    email_recipient: str | None = Field(default=None, max_length=254)
    email_topic: str | None = Field(default=None, max_length=240)


class RoutingResult(RouteDecision):
    provider: Literal["google"]
    backend: Literal["vertex", "developer"]
    model_used: str = Field(min_length=1, max_length=160)
    fallback_used: bool


class GeneratedReply(BaseModel):
    # Reserve the API's remaining 300 characters for graph-owned delivery status before any Gmail effect.
    response: str = Field(min_length=1, max_length=700)
    response_model_used: str = Field(min_length=1, max_length=160)
    response_fallback_used: bool


class Turn(TypedDict):
    user: str
    assistant: str


class AgentState(TypedDict):
    message: str
    history: NotRequired[list[Turn]]
    route: NotRequired[Route]
    reason: NotRequired[str]
    response: NotRequired[str]
    provider: NotRequired[str]
    backend: NotRequired[str]
    model_used: NotRequired[str]
    fallback_used: NotRequired[bool]
    delivery: NotRequired[str]
    email_recipient: NotRequired[str | None]
    email_topic: NotRequired[str | None]
    conversation_id: NotRequired[str]
    request_id: NotRequired[str | None]
    email_now: NotRequired[float]
    email_authorized: NotRequired[bool]
    email_dispatch: NotRequired[object]
    email_pending: NotRequired[dict | None]
    email_plan: NotRequired[dict | None]
    email: NotRequired[dict]
    email_subject: NotRequired[str]
    email_body: NotRequired[str]
    email_step: NotRequired[str]
    response_delivery: NotRequired[str]
    audio_capable: NotRequired[bool]
    response_model_used: NotRequired[str]
    response_fallback_used: NotRequired[bool]


class StructuredRouter(Protocol):
    async def ainvoke(self, messages: list[BaseMessage]) -> RoutingResult: ...


def build_graph(router: StructuredRouter | None, responder=None):
    """Compile the stateless graph; None allows diagram inspection only."""

    async def route_message(state: AgentState):
        if router is None:
            raise RuntimeError("A configured router is required to invoke the graph.")
        messages: list[BaseMessage] = [SystemMessage(content=ROUTER_PROMPT)]
        for turn in state.get("history", []):
            messages.extend([HumanMessage(content=turn["user"]), AIMessage(content=turn["assistant"])])
        messages.append(HumanMessage(content=state["message"]))
        decision = await router.ainvoke(messages)
        return {**decision.model_dump(), "email_pending": None}

    async def respond(state: AgentState):
        if responder is None:
            raise RuntimeError("A configured response provider is required.")
        delivery = state.get("response_delivery", "text")
        capable = state.get("audio_capable", False)
        messages = [SystemMessage(content=RESPONSE_PROMPT +
            f"\nCurrent response delivery: {delivery}\nAudio capable: {capable}\nRoute: {state['route']}")]
        for turn in state.get("history", []):
            messages.extend([HumanMessage(content=turn["user"]), AIMessage(content=turn["assistant"])])
        messages.append(HumanMessage(content=state["message"]))
        return (await responder.ainvoke(messages)).model_dump()

    def entry(state):
        pending = state.get("email_pending")
        if pending and state.get("email_now", 0) < pending["expires_at"] and pending_reply(pending, state["message"]):
            return "resume_email"
        return "route"

    async def resume_email(state):
        pending = state["email_pending"]
        reply = pending_reply(pending, state["message"])
        base = dict(pending["result"])
        if reply == "cancel" or reply == "restricted":
            restricted = reply == "restricted"
            return {**base, "email_pending": None, "email_step": "end",
                    "email": {"status": "not_attempted" if restricted else "cancelled", "delivery": "not_attempted"},
                    "response": ("El último mensaje restringe el envío. No se envió ningún correo y la solicitud pendiente quedó cancelada."
                                 if restricted else "Cancelé el correo pendiente. No se envió ningún mensaje.")}
        plan = {**pending["plan"], **reply}
        # A newly supplied topic needs a new answer, not the old clarifying response.
        # Keep the original action, route, authorization and recipient binding.
        if pending["needs"] == "content" and not reply.get("literal_body"):
            base.update(await respond({**state, **base}))
        return {**base, "email_plan": plan, "email_pending": None}

    def prepare_delivery(state):
        plan = state.get("email_plan") or initial_plan(state)
        if not plan:
            return {"email_step": "end", "email_pending": None}
        missing = "recipient" if not plan["recipient"] else "content" if not plan["content"] else None
        if missing:
            prompt = "¿A qué dirección de correo deseas que envíe la información?" if missing == "recipient" else "¿Sobre qué tema deseas que envíe la información?"
            pending = {"plan": plan, "needs": missing, "expires_at": plan["expires_at"],
                       "result": {key: state[key] for key in (*BASE_RESULT_KEYS,
                           "response_model_used", "response_fallback_used") if key in state}}
            return {"response": prompt, "email_pending": pending, "email_step": "end",
                    "email": {"status": "needs_" + missing, "delivery": "not_attempted"}}
        return {"email_plan": plan, "email_step": "format", "email_pending": None}

    def format_email(state):
        subject = {"preventa": "Información de preventa", "postventa": "Información de postventa",
                   "generica": "Información general"}[state["route"]]
        literal_hello = state["email_plan"].get("literal_body") == "Hola"
        body = "Hola" if literal_hello else "Hola,\n\n" + state["response"] + "\n\nSaludos."
        if literal_hello:
            subject = "Saludo"
        result = {"email_subject": subject, "email_body": body}
        if literal_hello:
            result["response"] = "Contenido del correo: Hola"
        if state["email_plan"]["mode"] == "draft":
            result.update(email={"status": "draft", "delivery": "not_attempted", "subject": subject, "body": body},
                          response="Borrador de correo. No se envió.\nAsunto: " + subject + "\n\n" + body)
        return result

    async def send_email(state):
        dispatch = state.get("email_dispatch")
        plan = state["email_plan"]
        if dispatch:
            outcome = await dispatch(recipient=plan["recipient"], subject=state["email_subject"], body=state["email_body"],
                                     conversation_id=state["conversation_id"], action_key=plan["action_key"],
                                     order_digest=plan["order_digest"], authorized=state.get("email_authorized", False))
        else:
            outcome = {"status": "disabled", "delivery": "not_attempted"}
        messages = {
            "accepted": "Gmail aceptó el correo. Su llegada al destinatario no está confirmada.",
            "unknown": "El resultado del envío es incierto. No se reintentará automáticamente; revisa el buzón del remitente.",
            "rejected": "Gmail rechazó el correo. No se reintentará automáticamente.",
            "disabled": "No se envió el correo: el envío real está desactivado.",
            "authorization_required": "No se envió el correo: falta la autorización del servidor para esta acción.",
            "configuration_required": "No se envió el correo: revisa la configuración de Gmail.",
            "action_conflict": "No se repitió el envío: ese identificador ya está asociado a otro contenido.",
            "not_attempted": "No se intentó el envío: revisa la autorización o el vencimiento del pedido.",
        }
        return {"email": outcome, "email_pending": None,
                "response": state["response"] + "\n\n" + messages.get(outcome["status"], messages["not_attempted"])}

    graph = StateGraph(AgentState)
    graph.add_node("route", route_message)
    graph.add_node("resume_email", resume_email)
    graph.add_node("prepare_delivery", prepare_delivery)
    graph.add_node("format_email", format_email)
    graph.add_node("send_email", send_email)
    graph.add_conditional_edges(START, entry, {"route": "route", "resume_email": "resume_email"})
    for route in ("preventa", "postventa", "generica"):
        graph.add_node(route, respond)
        graph.add_edge(route, "prepare_delivery")
    graph.add_conditional_edges("route", lambda state: state["route"], {
        route: route for route in ("preventa", "postventa", "generica")
    })
    graph.add_conditional_edges("resume_email", lambda state: state.get("email_step", "prepare"),
                                {"end": END, "prepare": "prepare_delivery"})
    graph.add_conditional_edges("prepare_delivery", lambda state: state["email_step"],
                                {"end": END, "format": "format_email"})
    graph.add_conditional_edges("format_email", lambda state: state["email_plan"]["mode"],
                                {"draft": END, "send": "send_email"})
    graph.add_edge("send_email", END)
    return graph.compile()

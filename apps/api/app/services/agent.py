from __future__ import annotations

import re

from app.auth import AuthUser
from app.definitions.access import AccessDenied, ProductAccess, authorize_product
from app.definitions.organizations import OrganizationDirectory
from app.engine.execution import ExecutionLedger, ExecutionOwner, principal_owner
from app.definitions.sessions import (
    DefinitionUnavailable,
    SessionEnded,
    check_pinned_session,
    pin_new_session,
)
from app.schemas import (
    IntentTrace,
    ProposedAction,
    RetrievedContext,
    SessionSummary,
    Signal,
    TurnRequest,
    TurnResponse,
)
from app.services.action_planner import ActionPlanner
from app.services.action_validator import ActionValidator
from app.services.agent_reasoner import AgentReasoner, AgentReasoningContext, AgentReasoningResult
from app.services.conversation_manager import ConversationManager
from app.services.demo_data import (
    extract_unknown_person,
    find_issue_by_id,
    find_issue_by_person,
    find_issue_by_person_in_scope,
    find_issues_by_person,
    find_issues_by_person_in_scope,
)
from app.services.intent_extractor import IntentExtractor
from app.services.language_normalizer import normalize_for_intent
from app.services.reasoning_policy import ReasoningPolicy
from app.services.retriever import ProductRetriever, RetrievedDocument
from app.services.session_manager import SessionManager
from app.workspace_config import get_workspace_scope, WorkspaceScope

# Words that follow "I'm" without being a name ("I'm not sure", "I'm looking for...").
_NOT_A_NAME = frozenset({
    "a", "an", "the", "not", "sure", "just", "looking", "trying", "going", "interested", "here",
    "fine", "good", "ok", "okay", "done", "back", "new", "from", "with", "checking", "curious",
    "wondering", "evaluating", "testing", "on", "in", "at", "so", "very", "really", "still",
})
# Words that follow "member" without being the person's name ("add a member to the team").
_MEMBER_FILLER = frozenset({"to", "in", "into", "for", "the", "workspace", "team", "directory",
                            "named", "called", "please", "who", "that"})


# A correction names what the visitor meant instead; the first matching subject wins.
_CORRECTIONS = (
    (r"\b(issue|issues|ticket|tickets|bug|bugs)\b", "OPEN_ISSUES", "Got it. I'll switch to Issues.", "Issues"),
    (r"\b(cycle|cycles|sprint|planning)\b", "OPEN_CYCLES", "Got it. I'll switch to Cycles.", "Cycles"),
    (r"\bgithub\b", "HIGHLIGHT_GITHUB_CARD", "Got it. I'll show GitHub instead.", "Integrations"),
    (r"\bslack\b", "HIGHLIGHT_SLACK_CARD", "Got it. I'll show Slack instead.", "Integrations"),
    (r"\b(project|projects)\b", "OPEN_PROJECTS", "Got it. I'll switch to Projects.", "Projects"),
    (r"\b(team|teams|members|people)\b", "OPEN_TEAMS", "Got it. I'll switch to Teams.", "Teams"),
)


def _explicit_issue_id(message: str) -> str | None:
    match = re.search(r"\b(?:LIN|PIX)-\d+\b", message, re.IGNORECASE)
    return match.group(0).upper() if match else None


def _asks_issue_change(text: str) -> bool:
    """An instruction to change a ticket's owner, priority or status (not a question about how)."""
    if re.search(r"\b(how|where|what|why|show|list)\b", text):
        return False
    verb = re.search(r"\b(make|set|change|mark|move|update|assign|reassign|bump|raise|lower)\b", text)
    value = re.search(r"\b(priority|urgent|critical|high|medium|low|done|review|todo|backlog|progress)\b", text)
    # "Assign it" with nobody named is a different question ("who should I assign it to?").
    assignment = re.search(r"\b(assign|reassign)\b.*\bto\s+[a-z]+", text)
    return bool(verb and (value or assignment))


def _stated_ticket_details(message: str, text: str) -> dict[str, str]:
    """Ticket details the visitor actually said: never a default."""
    details: dict[str, str] = {}
    if re.search(r"\b(urgent|critical|high)\b", text):
        details["priority"] = "High"
    elif re.search(r"\blow\b", text):
        details["priority"] = "Low"
    elif re.search(r"\bmedium\b", text):
        details["priority"] = "Medium"
    title = re.search(r"\b(?:about|regarding|named|called)\s+(.+)$", message, re.IGNORECASE)
    if title and title.group(1).strip():
        details["title"] = title.group(1).strip().rstrip(".!?").title()[:120]
    return details


def _member_named(name: str, members) -> str | None:
    """The one directory name a spoken name refers to: the full name or one of its parts."""
    wanted = name.strip().lower()
    for member in members:
        if wanted == member.lower() or wanted in member.lower().split():
            return member
    return None


def _format_names(names: list[str]) -> str:
    if not names:
        return "none"
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


class DemoAgent:
    def __init__(self) -> None:
        self.sessions = SessionManager()
        self.intent_extractor = IntentExtractor()
        self.reasoning_policy = ReasoningPolicy()
        self.action_planner = ActionPlanner()
        self.action_validator = ActionValidator()
        self.llm_reasoner = AgentReasoner()
        self.conversation_manager = ConversationManager()
        self.retriever = ProductRetriever()
        self.directory = OrganizationDirectory()

    def handle_turn(self, request: TurnRequest, principal: AuthUser,
                    data: dict | None = None) -> TurnResponse:
        """Run one turn of the Pixel for the product the principal is allowed to use."""
        try:
            access = authorize_product(principal, request.product_id, self.directory)
        except AccessDenied as denied:
            return self._denied_response(
                request,
                "That product is not available to you.",
                reason=f"Denied because the product cannot be used ({denied.reason}).",
            )

        workspace_scope = get_workspace_scope(request.workspace_scope_id, data)
        if workspace_scope is None:
            return self._denied_response(
                request,
                "I cannot run that workspace because it is not configured for this demo.",
                reason="Denied because the requested workspace scope is not configured.",
            )

        # A new session pins the product's current definition; an existing one keeps its pin.
        pin = None
        if not self.sessions.exists(request.session_id):
            try:
                pin = pin_new_session(access, self.directory.definitions)
            except DefinitionUnavailable as unavailable:
                return self._denied_response(
                    request,
                    "This product is not available right now. Please try again later.",
                    reason=f"Denied because no session can start ({unavailable.reason}).",
                )

        if not self.sessions.ensure_session(
            request.session_id,
            request.product_id,
            user_id=principal.user_id,
            tenant_id=principal.tenant_id,
            scope_id=request.workspace_scope_id,
            pin=pin,
            demo_context=principal.demo_context,
        ):
            return self._denied_response(
                request,
                "This conversation belongs to someone else. Start a new session to continue.",
                reason="Denied because the session is owned by another user or product.",
            )
        try:
            session_pin = check_pinned_session(self.sessions.pin_for(request.session_id), self.directory)
        except SessionEnded as ended:
            return self._denied_response(
                request,
                "This conversation has ended. Start a new conversation to continue.",
                reason=f"Denied because the session ended ({ended.reason}).",
            )
        definition_id = session_pin.definition_id
        if not self.sessions.activate_turn(
            request.session_id, request.turn_id,
            owner=principal_owner(principal, request.product_id), scope_id=request.workspace_scope_id,
        ):
            stale = self._stale_response(
                request,
                proposed_action=None,
                reason="Discarded because a newer turn already exists for this session.",
            )
            stale._engine_entered = False
            return stale
        self.sessions.store_message(
            request.session_id,
            request.turn_id,
            "user",
            request.message,
        )

        normalized_message = normalize_for_intent(request.message)
        conversational = self._conversational_turn(
            request, principal, workspace_scope, normalized_message, definition_id, data,
        )
        if conversational:
            return conversational
        direct_clarification = self._direct_clarification(
            request,
            workspace_scope,
            normalized_message,
        )
        if direct_clarification:
            return direct_clarification

        intent_trace, signals = self.intent_extractor.extract(request.message)
        intent_trace, signals = self.reasoning_policy.refine(
            request.message,
            intent_trace,
            signals,
        )
        clarification = self.conversation_manager.clarification_for(request.message)
        if clarification:
            self.sessions.remember_session_context(
                request.session_id,
                clarification.signals,
                clarification_pending=clarification.clarification_pending,
            )
            speech = clarification.speech
            if not self.sessions.is_active_turn(request.session_id, request.turn_id):
                return self._stale_response(
                    request,
                    proposed_action=None,
                    reason="Discarded because this is no longer the active turn.",
                )
            self.sessions.store_message(request.session_id, request.turn_id, "assistant", speech)
            self.sessions.complete_turn(request.session_id, request.turn_id)
            return TurnResponse(
                session_id=request.session_id,
                turn_id=request.turn_id,
                status="completed",
                speech=speech,
                proposed_action=None,
                validated_action=None,
                intent_trace=clarification.intent_trace,
                signals=clarification.signals,
                retrieved_context=[],
                session_summary=self.sessions.session_summary(
                    request.session_id,
                    clarification_pending=clarification.clarification_pending,
                ),
            )

        follow_up_action, follow_up_signals = self.conversation_manager.follow_up_action(
            request.message,
            last_feature=self.sessions.latest_signal_value(request.session_id, "feature_interest"),
            data=data,
        )
        signals.extend(follow_up_signals)
        proposed_action = self._hard_boundary_action(normalized_message) or follow_up_action
        llm_result: AgentReasoningResult | None = None
        llm_retrieved_docs: list[RetrievedDocument] = []
        llm_attempted = False
        if (
            proposed_action is None
            and self._should_try_llm_first(normalized_message, intent_trace)
        ):
            llm_attempted = True
            llm_result, llm_retrieved_docs = self._reason_with_llm(
                request,
                normalized_message,
                workspace_scope,
                principal.user_id,
                access,
                definition_id,
                data,
            )
            if llm_result:
                intent_trace, signals, proposed_action = self._apply_llm_result(
                    llm_result,
                    signals,
                )
        if proposed_action is None and not llm_result:
            proposed_action = self.action_planner.plan(
                request.message,
                intent_trace,
                selected_issue_id=request.selected_issue_id,
                allowed_issue_projects=set(workspace_scope.allowed_issue_projects),
                data=data,
            )
        if (
            proposed_action is None
            and not llm_attempted
        ):
            llm_attempted = True
            llm_result, llm_retrieved_docs = self._reason_with_llm(
                request,
                normalized_message,
                workspace_scope,
                principal.user_id,
                access,
                definition_id,
                data,
            )
            if llm_result:
                intent_trace, signals, proposed_action = self._apply_llm_result(
                    llm_result,
                    signals,
                )

        validated_action = self.action_validator.validate(
            definition_id,
            proposed_action,
            request.workspace_scope_id,
            data,
        )
        self._refine_issue_targeting(
            request.message, intent_trace, validated_action, workspace_scope, data
        )
        if validated_action:
            signals.extend(self._person_signals(request.message, workspace_scope, data))
        retrieved_docs = []
        if proposed_action and not validated_action:
            retrieved_docs = []
        elif llm_retrieved_docs:
            retrieved_docs = llm_retrieved_docs
        else:
            retrieved_docs = self.retriever.retrieve(definition_id, normalized_message)

        if not self.sessions.is_active_turn(request.session_id, request.turn_id):
            return self._stale_response(
                request,
                proposed_action=proposed_action,
                reason="Discarded because this is no longer the active turn.",
            )

        if proposed_action and not validated_action:
            intent_trace.status = "denied"
            intent_trace.reason = self._denied_reason(proposed_action.type, workspace_scope)
            speech = self._denied_speech(
                proposed_action.type, request.message, workspace_scope, data
            )
            status = "denied"
        else:
            speech = (
                llm_result.clarification_question
                if llm_result and not validated_action and llm_result.clarification_question
                else llm_result.speech
                if llm_result
                else self._speech(
                    request.message, intent_trace, validated_action, retrieved_docs,
                    workspace_scope, data,
                )
            )
            status = "completed"

        self.sessions.store_signals(request.session_id, request.turn_id, signals)
        self.sessions.remember_session_context(request.session_id, signals)
        self.sessions.store_message(request.session_id, request.turn_id, "assistant", speech)
        self.sessions.complete_turn(request.session_id, request.turn_id)

        return TurnResponse(
            session_id=request.session_id,
            turn_id=request.turn_id,
            status=status,
            speech=speech,
            proposed_action=proposed_action,
            validated_action=validated_action,
            intent_trace=intent_trace,
            signals=signals,
            retrieved_context=self._context_payload(retrieved_docs),
            session_summary=self.sessions.session_summary(request.session_id),
        )

    def cancel_turn(self, session_id: str, turn_id: int, owner: ExecutionOwner | None = None):
        return self.sessions.cancel_turn(session_id, turn_id, owner=owner)

    def _direct_clarification(
        self,
        request: TurnRequest,
        workspace_scope: WorkspaceScope,
        normalized_message: str,
    ) -> TurnResponse | None:
        speech: str | None = None
        reason = "The visitor's request needs a more specific target before Edith acts."

        if self._asks_vague_create_request(normalized_message):
            speech = "What should I create: a ticket, a project, a cycle, or a team member?"
            reason = "The visitor asked to create something but did not specify the object type."
        elif self._asks_vague_assignment_request(normalized_message):
            target = f" {request.selected_issue_id}" if request.selected_issue_id else " the current ticket"
            speech = f"Who should I assign{target} to? You can name someone in this workspace."
            reason = "The visitor asked to assign work but did not name an assignee."
        elif self._asks_broad_workspace_request(normalized_message):
            speech = (
                f"I can only show work inside {workspace_scope.name}. "
                "Switch the workspace scope first, then ask me again."
            )
            reason = "The visitor asked for broad company or other-workspace data."

        if speech is None:
            return None

        intent_trace = IntentTrace(
            goal="Clarify request",
            current_intent="Clarification needed",
            relevant_feature="Dashboard",
            reason=reason,
            confidence=0.68,
            status="active",
        )
        if not self.sessions.is_active_turn(request.session_id, request.turn_id):
            return self._stale_response(
                request,
                proposed_action=None,
                reason="Discarded because this is no longer the active turn.",
            )
        self.sessions.store_message(request.session_id, request.turn_id, "assistant", speech)
        self.sessions.complete_turn(request.session_id, request.turn_id)
        return TurnResponse(
            session_id=request.session_id,
            turn_id=request.turn_id,
            status="completed",
            speech=speech,
            proposed_action=None,
            validated_action=None,
            intent_trace=intent_trace,
            signals=[],
            retrieved_context=[],
            session_summary=self.sessions.session_summary(request.session_id),
        )

    def _conversational_turn(
        self,
        request: TurnRequest,
        principal: AuthUser,
        workspace_scope: WorkspaceScope,
        text: str,
        definition_id: str,
        data: dict | None,
    ) -> TurnResponse | None:
        """Conversation the browser used to answer by itself before 5c (plan, section 2.3).

        Every visitor message now reaches this service; the browser only executes what comes back.
        An action proposed here goes through the same validator as any other.
        """
        reply = self._conversational_reply(request, principal, workspace_scope, text, data)
        if reply is None:
            return None
        speech, action, feature, *rest = reply
        status = rest[0] if rest else "completed"
        # A question back to the visitor attaches no documents, as a clarification never has.
        evidence = rest[1] if len(rest) > 1 else True
        validated = None
        if action is not None:
            validated = self.action_validator.validate(
                definition_id, action, request.workspace_scope_id, data,
            )
            if validated is None:
                # Not allowed here: the ordinary path decides and explains the refusal.
                return None
        if not self.sessions.is_active_turn(request.session_id, request.turn_id):
            return self._stale_response(
                request, proposed_action=action,
                reason="Discarded because this is no longer the active turn.",
            )
        # Signals and evidence are recorded exactly as for any other turn; only the decision is new.
        trace, signals = self.intent_extractor.extract(request.message)
        trace, signals = self.reasoning_policy.refine(request.message, trace, signals)
        self._refine_issue_targeting(request.message, trace, validated, workspace_scope, data)
        if feature:
            trace.relevant_feature = feature
        if status == "denied":
            trace.status = "denied"
        if validated:
            signals.extend(self._person_signals(request.message, workspace_scope, data))
        retrieved = self.retriever.retrieve(definition_id, text) if status != "denied" and evidence else []
        self.sessions.store_signals(request.session_id, request.turn_id, signals)
        self.sessions.remember_session_context(request.session_id, signals)
        self.sessions.store_message(request.session_id, request.turn_id, "assistant", speech)
        self.sessions.complete_turn(request.session_id, request.turn_id)
        return TurnResponse(
            session_id=request.session_id,
            turn_id=request.turn_id,
            status=status,
            speech=speech,
            proposed_action=action,
            validated_action=validated,
            intent_trace=trace,
            signals=signals,
            retrieved_context=self._context_payload(retrieved),
            session_summary=self.sessions.session_summary(request.session_id),
        )

    def _conversational_reply(
        self,
        request: TurnRequest,
        principal: AuthUser,
        workspace_scope: WorkspaceScope,
        text: str,
        data: dict | None,
    ) -> tuple | None:
        """(speech, action or None, feature or None[, status]) for a conversational turn, or None."""
        message = request.message
        # This engine is given only the selected workspace's records, so anyone else, in another
        # workspace or nowhere at all, reads the same: unknown.
        scoped = workspace_scope.allowed_team_members

        # "Open a ticket for Maya and assign to Jen": the assignee decides, and must exist first.
        if (re.search(r"\b(open|start|draft)\b", text) and re.search(r"\b(assign|assigned|owner)\b", text)
                and re.search(r"\b(ticket|issue|bug)\b", text)):
            match = re.search(r"\b(?:assign(?:ed)?\s+(?:it\s+|this\s+)?to|owner is|assignee is)\s+([a-zA-Z]+)",
                              message, re.IGNORECASE)
            if match:
                wanted = match.group(1).capitalize()
                known = _member_named(wanted, scoped)
                if known is None:
                    return (
                        f"{wanted} is not in the team directory yet. I'll open Teams so you can add {wanted} first.",
                        ProposedAction(type="HIGHLIGHT_ADD_MEMBER_BUTTON", payload={"name": wanted}),
                        "Teams",
                    )
                # The form is prefilled only with what the visitor said; it creates nothing.
                prefill = {"assignee": known, **_stated_ticket_details(message, text)}
                return (
                    f"I'll open the ticket form and prefill {known}. Review the details, then create the ticket.",
                    ProposedAction(type="HIGHLIGHT_CREATE_TICKET_BUTTON", payload=prefill),
                    "Issues", "completed", False,
                )

        # "Assign it to Priya" when Priya is not someone here: add her first, never guess another person.
        target_issue = _explicit_issue_id(message) or request.selected_issue_id
        if target_issue and re.search(r"\b(assign|reassign)\b", text) and not self._asks_incomplete_ticket_create(text):
            match = re.search(r"\b(?:assign(?:ed)?\s+(?:it\s+|this\s+)?to|owner is|assignee is)\s+([a-zA-Z]+)",
                              message, re.IGNORECASE)
            if match and _member_named(match.group(1), scoped) is None:
                wanted = match.group(1).capitalize()
                return (
                    f"{wanted} is not in the team directory yet. I'll open Teams so you can add {wanted} "
                    f"before assigning {target_issue}.",
                    ProposedAction(type="HIGHLIGHT_ADD_MEMBER_BUTTON", payload={"name": wanted}),
                    "Teams",
                )

        # "Make it high priority" with no ticket open and none named: ask which one. In "assign it to
        # Noah" Noah is the new owner, not the ticket, so "it" with nothing open is always asked about.
        refers_to_open_ticket = re.search(r"\b(it|this|that)\b", text)
        if (not target_issue and _asks_issue_change(text) and (
                refers_to_open_ticket
                or find_issues_by_person_in_scope(message, set(workspace_scope.allowed_project_ids),
                                                  set(workspace_scope.allowed_issue_projects), data) == ())):
            owners = sorted(scoped)[:2]
            options = ", ".join(f"{name.split()[0]}'s ticket" for name in owners)
            return (
                f"Which ticket should I update: {options + ', or ' if options else ''}the issue currently open?",
                None, None, "completed", False,
            )

        correction = self._correction(message, text)
        if correction is not None:
            return correction

        if self._asks_evaluator_path(text):
            return (
                "Here is a clean guided path: start with sprint planning, open Maya's ticket, "
                "assign it to Noah, create a ticket for a new teammate, then try Salesforce to prove guardrails.",
                ProposedAction(type="OPEN_DASHBOARD"),
                "Dashboard",
            )
        # "I'm an engineering manager moving from Jira": context about the visitor, not a request.
        # It is acknowledged (and recorded as signals below), never read as navigation.
        about_visitor = re.match(r"^\s*(i am|i'm|im|we are|we're)\s+(a|an)\s+\w+", message.lower().replace("’", "'"))
        asks_something = re.search(
            r"\b(show|open|create|assign|make|set|list|find|filter|go|take|add|update|mark|how|what|where)\b", text)
        if about_visitor and not asks_something:
            if re.search(r"\b(lost|confused|stuck|unsure)\b", text):
                return self._next_step_speech(request.current_page, workspace_scope.name), None, None
            return (
                "Thanks, that helps. What would you like to explore first: planning, tickets, projects, "
                "teams, or integrations?",
                None, None,
            )
        name = self._introduced_name(message)
        if name:
            return (
                f"Nice to meet you, {name}. What would you like to explore first: "
                "planning, tickets, projects, teams, or integrations?",
                None, None,
            )
        if self._asks_capabilities(message):
            return (
                f"I can guide this Pixel demo through {workspace_scope.name}: planning, issues, projects, "
                "teams, and integrations. I can open views, find tickets, update issue fields, create demo "
                "records, and block work outside this scope.",
                None, None,
            )
        if self._asks_identity(text):
            return (
                "I'm Edith, Pixel's live demo guide. I can walk you through planning, tickets, projects, "
                "teams, and integrations inside this workspace.",
                None, None,
            )
        if self._is_plain_greeting(text):
            remembered = self._remembered_visitor_name(request.session_id)
            if remembered:
                return f"Hi {remembered}. What would you like to explore next in Pixel?", None, None
            return "Hi there. What would you like to explore first in Pixel?", None, None
        member = self._member_request(message, text)
        if member is not None:
            if member:
                return (
                    f"I'll open Teams so you can add {member} to this workspace.",
                    ProposedAction(type="HIGHLIGHT_ADD_MEMBER_BUTTON", payload={"name": member}),
                    "Teams",
                )
            return (
                "Who should I add to the team directory?",
                ProposedAction(type="HIGHLIGHT_ADD_MEMBER_BUTTON"),
                "Teams",
            )
        if self._asks_incomplete_ticket_create(text):
            return (
                "Who should own this ticket? Name a teammate in this workspace and I'll prefill the ticket form.",
                ProposedAction(type="HIGHLIGHT_CREATE_TICKET_BUTTON"),
                "Issues", "completed", False,
            )
        if self._asks_next_step(text):
            return self._next_step_speech(request.current_page, workspace_scope.name), None, None
        if self._asks_what_changed(text):
            return self._last_change_speech(request, principal), None, None
        if self._asks_project_count(text):
            projects = [
                project["name"] for project in (data or {}).get("projects", [])
                if project.get("id") in workspace_scope.allowed_project_ids
            ]
            return (
                f"{workspace_scope.name} has {len(projects)} visible projects: {_format_names(projects)}. "
                "I'll open Projects.",
                ProposedAction(type="OPEN_PROJECTS"),
                "Projects",
            )
        # "What about Noah" or "show Avery's tickets": every ticket that person owns here.
        if re.search(r"\bwhat about\b", text) or (
            re.search(r"\b(all|list|show)\b", text) and re.search(r"\b(tickets|issues|bugs)\b", text)
        ):
            issues = find_issues_by_person_in_scope(
                message, set(workspace_scope.allowed_project_ids),
                set(workspace_scope.allowed_issue_projects), data,
            )
            if issues:
                assignee = issues[0].assignee
                plural = "ticket" if len(issues) == 1 else "tickets"
                return (
                    f"I found {len(issues)} {plural} assigned to {assignee}: "
                    f"{', '.join(issue.id for issue in issues)}. I'll show those issues.",
                    ProposedAction(type="FILTER_ISSUES_BY_ASSIGNEE", payload={"assignee": assignee}),
                    "Issues",
                )
        return None

    def _correction(self, message: str, text: str) -> tuple | None:
        # The normalizer may already have dropped the rejected clause, so the cue is read raw.
        if not re.search(r"\b(no|not|instead|rather)\b", message.lower()):
            return None
        # "not cycles, show me the issues": what was rejected is dropped before choosing.
        wanted = re.sub(r"\b(?:not|no|instead of|rather than)(?:\s+(?:not|no))*\s+(?:the\s+)?\w+", " ", text)
        for pattern, action, speech, feature in _CORRECTIONS:
            if re.search(pattern, wanted):
                return speech, ProposedAction(type=action), feature
        return None

    def _asks_evaluator_path(self, text: str) -> bool:
        return bool(
            re.search(r"\b(evaluator|judge|reviewer|demo path|demo script|test script)\b", text)
        )

    def _introduced_name(self, message: str) -> str | None:
        match = re.search(r"\b(?:i am|i'm|im|my name is|call me)\s+([a-z]+)", message.lower().replace("’", "'"))
        if not match or match.group(1) in _NOT_A_NAME:
            return None
        return match.group(1).capitalize()

    def _remembered_visitor_name(self, session_id: str) -> str | None:
        for message in self.sessions.recent_user_messages(session_id):
            name = self._introduced_name(message)
            if name:
                return name
        return None

    def _asks_identity(self, text: str) -> bool:
        return bool(
            re.search(r"\b(who are you|who r you|who are u|who r u|what are you|your name|who is edith)\b", text)
            or (re.search(r"\b(hi+|hello|hey)\b", text) and re.search(r"\b(who|what)\b", text))
        )

    def _is_plain_greeting(self, text: str) -> bool:
        return bool(re.fullmatch(r"(hi+|hello|hey)( there)?[?!. ]*", text.strip()))

    def _member_request(self, message: str, text: str) -> str | None:
        """The name of a person to add to the team directory ('' if unnamed), or None."""
        if not re.search(r"\b(add|create|invite|new)\b", text):
            return None
        if not re.search(r"\b(member|teammate|person|user|employee)\b", text):
            return None
        if re.search(r"\b(ticket|tickets|issue|issues|bug|bugs)\b", text):
            return None
        for pattern in (r"\b(?:called|named)\s+", r"\b(?:member|teammate|person|user|employee)\s+"):
            match = re.search(pattern, message, re.IGNORECASE)
            if match is None:
                continue
            words = re.findall(r"[A-Za-z]+", message[match.end():])[:2]
            if words and words[0].lower() not in _MEMBER_FILLER:
                # A second capitalized word is the surname ("Priya Shah"); anything else is not.
                if len(words) == 2 and words[1][0].isupper() and words[1].lower() not in _MEMBER_FILLER:
                    return f"{words[0].capitalize()} {words[1]}"
                return words[0].capitalize()
        return ""

    def _asks_next_step(self, text: str) -> bool:
        return bool(re.search(
            r"\b(what next|next step|what should i try|what should we try|where should i go|guide me|walk me through)\b",
            text,
        ))

    def _next_step_speech(self, current_page: str | None, workspace_name: str) -> str:
        if current_page == "dashboard":
            return (f"A strong next move is sprint planning. Ask me to show planning, and I'll open the "
                    f"current cycle for {workspace_name}.")
        if current_page in {"issues", "issue_detail"}:
            return ("Next, try a follow-up like assign it to Noah, make it high priority, or show all "
                    "tickets for Maya.")
        if current_page == "integrations":
            return ("A good next step is GitHub. Ask me how GitHub works or tell me to set it up, and "
                    "I'll show the repository workflow.")
        if current_page == "teams":
            return ("From here, add a team member or ask me to create a ticket for someone new. I'll "
                    "keep the assignment inside this workspace.")
        if current_page == "projects":
            return ("Try creating a project or ask which projects are visible. I'll keep the roadmap "
                    "scoped to this workspace.")
        return ("Try asking about tickets, planning, GitHub, or creating work. I'll move the workspace "
                "and explain what changed.")

    def _asks_what_changed(self, text: str) -> bool:
        return bool(re.search(
            r"\b(what did we .*change|what changed|what just happened|what did you update|what did you change)\b",
            text,
        ))

    def _last_change_speech(self, request: TurnRequest, principal: AuthUser) -> str:
        """Only a change the ledger committed for this caller, session and workspace (5b, 8.4)."""
        change = ExecutionLedger().last_executed(
            principal_owner(principal, request.product_id), request.session_id,
            request.workspace_scope_id,
        )
        if change is None:
            return "Nothing has changed in this conversation yet."
        verb = "created" if change.capability == "CREATE_RECORD" else "updated"
        return f"The most recent change: {verb} {change.record_id or 'a record'}."

    def _asks_project_count(self, text: str) -> bool:
        return bool(
            re.search(r"\b(how many|count|number of|what projects|which projects)\b", text)
            and re.search(r"\b(project|projects)\b", text)
        )

    def _denied_reason(self, proposed_action_type: str, workspace_scope: WorkspaceScope) -> str:
        if proposed_action_type in {
            "OPEN_DEMO_ISSUE",
            "UPDATE_DEMO_ISSUE",
            "FILTER_ISSUES_BY_ASSIGNEE",
            "HIGHLIGHT_ASSIGNMENT_CONTROL",
            "CREATE_DEMO_ISSUE",
        }:
            return f"Denied because the requested object is outside {workspace_scope.name}."
        return "Denied because the requested action is outside this product demo."

    def _denied_speech(
        self,
        proposed_action_type: str,
        message: str,
        workspace_scope: WorkspaceScope,
        data: dict | None = None,
    ) -> str:
        if proposed_action_type in {
            "OPEN_DEMO_ISSUE",
            "UPDATE_DEMO_ISSUE",
            "FILTER_ISSUES_BY_ASSIGNEE",
            "HIGHLIGHT_ASSIGNMENT_CONTROL",
            "CREATE_DEMO_ISSUE",
        }:
            issue = find_issue_by_person(message, data)
            if issue:
                return (
                    f"{issue.assignee} is outside {workspace_scope.name}, "
                    "so I cannot show or change that work here."
                )
            return f"That work is outside {workspace_scope.name}, so I cannot show or change it here."
        return "I can only demonstrate Pixel workflows here, so I cannot open that."

    def _stale_response(self, request: TurnRequest, proposed_action, reason: str) -> TurnResponse:
        return TurnResponse(
            session_id=request.session_id,
            turn_id=request.turn_id,
            status="stale",
            speech="This turn was replaced by a newer request.",
            proposed_action=proposed_action,
            validated_action=None,
            intent_trace=IntentTrace(
                status="interrupted",
                reason=reason,
            ),
            signals=[],
            retrieved_context=[],
            session_summary=(
                self.sessions.session_summary(request.session_id)
                if self.sessions.exists(request.session_id)
                else SessionSummary()
            ),
        )

    def _speech(
        self,
        message: str,
        intent_trace: IntentTrace,
        validated_action,
        retrieved_docs: list[RetrievedDocument],
        workspace_scope: WorkspaceScope,
        data: dict | None = None,
    ) -> str:
        if validated_action and validated_action.type == "OPEN_SYSTEM_ARCHITECTURE":
            return (
                "I'll open the system architecture view so you can see how Pixel listens, "
                "checks project scope, validates actions, and updates the workspace."
            )

        if validated_action and validated_action.type == "OPEN_DEMO_ISSUE":
            issue_id = validated_action.payload.get("issue_id")
            issue = find_issue_by_id(issue_id, data) if isinstance(issue_id, str) else None
            if issue:
                return f"I found {issue.id}, assigned to {issue.assignee}. I'll open that ticket."

        if validated_action and validated_action.type == "CREATE_DEMO_ISSUE":
            payload = validated_action.payload
            assignee = payload.get("assignee", "")
            known_team = self._known_team_members(workspace_scope)
            if assignee and assignee not in known_team:
                return (
                    f"{assignee} is not in the team directory yet. "
                    f"I'll open Teams so you can add {assignee} first."
                )
            return (
                f"I created {payload['id']} and assigned it to {payload['assignee']}. "
                "I'll open the new demo ticket."
            )

        if validated_action and validated_action.type == "UPDATE_DEMO_ISSUE":
            payload = validated_action.payload
            changes = []
            if payload.get("assignee"):
                changes.append(f"assignee is now {payload['assignee']}")
            if payload.get("priority"):
                changes.append(f"priority is now {payload['priority']}")
            if payload.get("status"):
                changes.append(f"status is now {payload['status']}")
            change_text = ", ".join(changes)
            return f"Done. I updated {payload['issue_id']}: {change_text}."

        if validated_action and validated_action.type == "FILTER_ISSUES_BY_ASSIGNEE":
            issues = find_issues_by_person_in_scope(
                message,
                set(workspace_scope.allowed_project_ids),
                set(workspace_scope.allowed_issue_projects),
                data,
            )
            assignee = validated_action.payload.get("assignee")
            if issues and isinstance(assignee, str):
                issue_list = ", ".join(issue.id for issue in issues)
                plural = "ticket" if len(issues) == 1 else "tickets"
                return (
                    f"I found {len(issues)} {plural} assigned to {assignee}: "
                    f"{issue_list}. I'll show the filtered issue list."
                )
            return "I'll show the filtered issue list."

        if validated_action and validated_action.type == "HIGHLIGHT_CREATE_TICKET_BUTTON":
            return self._grounded_prefix(retrieved_docs) + (
                "You create tickets from the Issues view. I'll open it and highlight Create ticket."
            )

        if validated_action and validated_action.type == "HIGHLIGHT_ADD_MEMBER_BUTTON":
            name = validated_action.payload.get("name", "that person")
            return (
                f"{name} is not in the team directory yet. "
                f"I'll open Teams so you can add {name} first."
            )

        if validated_action and validated_action.type == "HIGHLIGHT_ASSIGNMENT_CONTROL":
            issue_id = validated_action.payload.get("issue_id")
            issue = find_issue_by_id(issue_id, data) if isinstance(issue_id, str) else find_issue_by_person_in_scope(
                message,
                set(workspace_scope.allowed_project_ids),
                set(workspace_scope.allowed_issue_projects),
                data,
            )
            assignment_context = self._grounded_sentence(
                retrieved_docs,
                ("assignment", "assignee", "assigned"),
            )
            if issue:
                return assignment_context + (
                    f"I'll open {issue.id} and highlight the assignee control for {issue.assignee}."
                )
            return assignment_context + "I'll open a demo issue and highlight the assignee control."

        unknown_person = extract_unknown_person(message)
        if unknown_person and intent_trace.relevant_feature == "Issues":
            return self._grounded_prefix(retrieved_docs) + (
                f"I could not find a ticket for {unknown_person}, so I'll show the Issues list."
            )

        if self._asks_capabilities(message):
            return (
                "I can guide this Pixel demo through planning, issues, projects, "
                "teams, and integrations. I can open approved views, find demo issues, "
                "highlight controls, answer workflow questions, and block requests outside this product."
            )

        if self._is_greeting(message):
            name = self._extract_visitor_name(message)
            if name:
                return f"Hey {name}! Welcome to Pixel. What would you like to explore first — planning, issues, projects, teams, or integrations?"
            return "Hey there! Welcome to Pixel. What would you like to explore — planning, issues, projects, teams, or integrations?"

        if intent_trace.relevant_feature == "Cycles":
            return self._grounded_prefix(retrieved_docs) + "I'll show you the current cycle."
        if intent_trace.relevant_feature == "Issues":
            return self._grounded_prefix(retrieved_docs) + "I'll open Issues."
        if intent_trace.relevant_feature == "Projects":
            return self._grounded_prefix(retrieved_docs) + "I'll open Projects."
        if intent_trace.relevant_feature == "Teams":
            if self._asks_team_count(message):
                count = self._team_count(workspace_scope)
                return f"There are {count} team members in {workspace_scope.name}. I'll open Teams."
            return self._grounded_prefix(retrieved_docs) + "I'll open Teams."
        if intent_trace.relevant_feature == "Integrations":
            if validated_action and validated_action.type == "OPEN_GITHUB_SETUP":
                return self._grounded_prefix(retrieved_docs) + (
                    "I'll open the GitHub setup flow and show the connection steps."
                )
            if validated_action and validated_action.type == "HIGHLIGHT_GITHUB_CARD":
                return (
                    "GitHub keeps pull requests, commits, and issue references connected to the work. "
                    "I'll open Integrations and highlight GitHub."
                )
            if validated_action and validated_action.type == "HIGHLIGHT_SLACK_CARD":
                return (
                    "Slack lets teams create issues from messages and receive workflow updates where conversations happen. "
                    "I'll open Integrations and highlight Slack."
                )
            return self._grounded_prefix(retrieved_docs) + "I'll open Integrations."
        if intent_trace.relevant_feature == "Voice":
            return (
                "When you start speaking, I stop the current response, listen for the completed thought, "
                "then run the new request through the same scoped action checks."
            )
        return "I can help demonstrate planning, issues, projects, teams, and integrations in this product."

    def _person_signals(self, message: str, workspace_scope: WorkspaceScope,
                        data: dict | None = None) -> list[Signal]:
        issue = find_issue_by_person_in_scope(
            message,
            set(workspace_scope.allowed_project_ids),
            set(workspace_scope.allowed_issue_projects),
            data,
        )
        if not issue:
            return []
        return [Signal(type="person_interest", value=issue.assignee, confidence=0.84)]

    def _signals_from_trace(self, intent_trace: IntentTrace) -> list[Signal]:
        signals: list[Signal] = []
        if intent_trace.relevant_feature:
            signals.append(
                Signal(
                    type="feature_interest",
                    value=intent_trace.relevant_feature.lower(),
                    confidence=max(intent_trace.confidence, 0.7),
                )
            )
        if intent_trace.pain_point:
            signals.append(
                Signal(
                    type="pain_point",
                    value=intent_trace.pain_point.lower(),
                    confidence=max(intent_trace.confidence, 0.7),
                )
            )
        return signals

    def _hard_boundary_action(self, normalized_message: str) -> ProposedAction | None:
        if self._mentions_external_crm(normalized_message):
            return ProposedAction(type="OPEN_SALESFORCE")
        if self._mentions_external_email(normalized_message):
            return ProposedAction(type="OPEN_GMAIL")
        if self._mentions_destructive_operation(normalized_message):
            return ProposedAction(type="DELETE_ISSUES")
        return None

    def _mentions_external_crm(self, text: str) -> bool:
        return bool(re.search(r"\b(salesforce|crm|opportunit(?:y|ies)|leads?)\b", text))

    def _mentions_external_email(self, text: str) -> bool:
        if "gmail" in text:
            return True
        return bool(
            re.search(r"\b(open|pull up|check|read|show)\b", text)
            and re.search(r"\b(email|inbox)\b", text)
        )

    def _mentions_destructive_operation(self, text: str) -> bool:
        return bool(
            re.search(r"\b(delete|erase|wipe|clear)\b", text)
            or "remove all" in text
            or "delete all" in text
        )

    def _reason_with_llm(
        self,
        request: TurnRequest,
        normalized_message: str,
        workspace_scope: WorkspaceScope,
        user_id: str,
        access: ProductAccess,
        definition_id: str,
        data: dict | None = None,
    ) -> tuple[AgentReasoningResult | None, list[RetrievedDocument]]:
        # Per-session, per-user and per-deployment limits are enforced by the usage ledger.
        if not self.llm_reasoner.enabled():
            return None, []

        retrieved_docs = self.retriever.retrieve(definition_id, normalized_message)
        llm_result = self.llm_reasoner.reason(
            AgentReasoningContext(
                owner=access.context,
                definition_id=definition_id,
                message=request.message,
                current_page=request.current_page,
                selected_issue_id=request.selected_issue_id,
                workspace_scope=workspace_scope,
                retrieved_docs=retrieved_docs,
                user_id=user_id,
                session_id=request.session_id,
                request_id=f"turn:{request.session_id}:{request.turn_id}",
                visible_data=data,
            )
        )
        return llm_result, retrieved_docs

    def _apply_llm_result(
        self,
        llm_result: AgentReasoningResult,
        signals: list[Signal],
    ) -> tuple[IntentTrace, list[Signal], ProposedAction | None]:
        intent_trace = llm_result.intent_trace
        updated_signals = [signal for signal in signals if signal.type != "feature_interest"]
        updated_signals.extend(self._signals_from_trace(intent_trace))
        proposed_action = None
        if llm_result.proposed_action:
            proposed_action = ProposedAction(
                type=llm_result.proposed_action.type,
                payload=llm_result.proposed_action.payload,
            )
        return intent_trace, updated_signals, proposed_action

    def _should_try_llm_first(self, normalized_message: str, intent_trace: IntentTrace) -> bool:
        if intent_trace.relevant_feature is not None:
            return False
        if re.search(r"\b(open|show|create|make|add|assign|reassign|set up|setup|connect)\b", normalized_message):
            return False
        if any(
            phrase in normalized_message
            for phrase in (
                "week by week",
                "each week",
                "next batch of work",
                "how does my team",
                "how should my team",
                "best practice",
                "what is the best way",
                "workflow",
                "process",
            )
        ):
            return True
        return intent_trace.relevant_feature is None

    def _team_count(self, workspace_scope: WorkspaceScope) -> int:
        return len(self._known_team_members(workspace_scope))

    def _known_team_members(self, workspace_scope: WorkspaceScope) -> set[str]:
        return set(workspace_scope.allowed_team_members)

    def _asks_team_count(self, message: str) -> bool:
        text = normalize_for_intent(message)
        return (
            "how many" in text
            and any(term in text for term in ("team", "member", "members", "people"))
        )

    def _asks_capabilities(self, message: str) -> bool:
        text = normalize_for_intent(message)
        return any(
            phrase in text
            for phrase in (
                "are you capable",
                "what can you do",
                "what are you able",
                "what can this do",
                "can you do",
            )
        )

    def _is_greeting(self, message: str) -> bool:
        text = normalize_for_intent(message)
        greetings = (
            "hi", "hii", "hiii", "hello", "hey", "howdy", "greetings",
            "good morning", "good afternoon", "good evening",
            "whats up", "sup", "yo",
        )
        stripped = text.strip()
        if stripped in greetings:
            return True
        if any(
            stripped.startswith(g + " ") or stripped.startswith(g + " there")
            for g in greetings
        ):
            return True
        return bool(re.match(r"^(?:hi+|hey+|hello+|yo)\b", stripped))

    def _extract_visitor_name(self, message: str) -> str | None:
        text = normalize_for_intent(message)
        match = re.search(
            r"\b(?:i am|im|i m|my name is|this is|its|it s|call me)\s+([a-z][a-z]+)",
            text,
        )
        if match:
            return match.group(1).capitalize()
        return None

    def _asks_vague_create_request(self, text: str) -> bool:
        return bool(
            re.search(r"\b(create|make|add|new)\b", text)
            and not re.search(
                r"\b(it|this|that|priority|status|urgent|critical|high|medium|low|done|review|todo|backlog)\b",
                text,
            )
            and not re.search(
                r"\b(ticket|tickets|issue|issues|bug|bugs|project|projects|cycle|cycles|sprint|member|members|teammate|teammates|person|people)\b",
                text,
            )
        )

    def _asks_incomplete_ticket_create(self, text: str) -> bool:
        creates = re.search(r"\b(create|make|add|raise|file)\b", text) or (
            re.search(r"\b(open|start|draft)\b", text) and re.search(r"\b(new|fresh)\b", text)
        )
        # "Make Noah's ticket high priority" changes an existing ticket; it creates nothing.
        if (re.search(r"\b(priority|urgent|critical|status|done|review|todo|backlog)\b", text)
                and not re.search(r"\b(create|new|fresh|raise|file|add)\b", text)):
            return False
        return bool(
            creates
            and re.search(r"\b(ticket|tickets|issue|issues|bug|bugs)\b", text)
            and not re.search(r"\b(where|how|best way|show me how|show how)\b", text)
            and not re.search(r"\b(for|assigned to|assign to|owner is|assignee is)\b", text)
        )

    def _asks_vague_assignment_request(self, text: str) -> bool:
        return bool(
            re.search(r"\b(assign|reassign|owner|assignee)\b", text)
            and not re.search(r"\b(to|for|maya|noah|avery|iris|lucifer)\b", text)
            and not re.search(r"\b(how do i assign|how to assign|show assignment|issue assignment)\b", text)
        )

    def _asks_broad_workspace_request(self, text: str) -> bool:
        return bool(
            re.search(
                r"\b(company|organization|org|other workspace|other project|another workspace|another team|all workspaces|every workspace|all teams|every team)\b",
                text,
            )
            and re.search(r"\b(project|projects|ticket|tickets|issue|issues|work|team|teams)\b", text)
        )

    def _mentions_new_issue_workflow(self, message: str) -> bool:
        text = normalize_for_intent(message)
        return (
            any(phrase in text for phrase in ("best way", "where", "how do i", "how to", "show me how", "show how"))
            and any(term in text for term in ("create", "new", "fresh", "make", "add", "raise", "file"))
            and any(term in text for term in ("ticket", "issue", "bug"))
        )

    def _refine_issue_targeting(
        self,
        message: str,
        intent_trace: IntentTrace,
        validated_action,
        workspace_scope: WorkspaceScope,
        data: dict | None = None,
    ) -> None:
        if validated_action and validated_action.type == "OPEN_SYSTEM_ARCHITECTURE":
            intent_trace.goal = "System architecture"
            intent_trace.current_intent = "Open architecture view"
            intent_trace.relevant_feature = "Architecture"
            intent_trace.reason = "Opening the product architecture view through an approved demo action."
            intent_trace.confidence = 0.9
            return

        if validated_action and validated_action.type in {
            "OPEN_DEMO_ISSUE",
            "HIGHLIGHT_ASSIGNMENT_CONTROL",
            "CREATE_DEMO_ISSUE",
            "UPDATE_DEMO_ISSUE",
        }:
            if validated_action.type == "UPDATE_DEMO_ISSUE":
                payload = validated_action.payload
                intent_trace.goal = "Update issue"
                intent_trace.current_intent = "Edit issue metadata"
                intent_trace.relevant_feature = "Issues"
                intent_trace.reason = f"Updating {payload.get('issue_id')} through an approved demo action."
                intent_trace.confidence = 0.86
                return

            if validated_action.type == "CREATE_DEMO_ISSUE":
                assignee = validated_action.payload.get("assignee")
                issue_id = validated_action.payload.get("id")
                intent_trace.goal = "Create issue"
                intent_trace.current_intent = "Create demo issue"
                intent_trace.relevant_feature = "Issues"
                intent_trace.reason = f"Creating {issue_id} as a safe demo ticket assigned to {assignee}."
                intent_trace.confidence = 0.88
                return

            issue_id = validated_action.payload.get("issue_id")
            issue = find_issue_by_id(issue_id, data) if isinstance(issue_id, str) else find_issue_by_person_in_scope(
                message,
                set(workspace_scope.allowed_project_ids),
                set(workspace_scope.allowed_issue_projects),
                data,
            )
            if issue:
                intent_trace.goal = "Issue lookup"
                intent_trace.current_intent = (
                    "Highlight assignment control"
                    if validated_action.type == "HIGHLIGHT_ASSIGNMENT_CONTROL"
                    else "Open specific issue"
                )
                intent_trace.relevant_feature = "Issues"
                intent_trace.reason = (
                    f"Highlighting the assignee control on {issue.id} for {issue.assignee}."
                    if validated_action.type == "HIGHLIGHT_ASSIGNMENT_CONTROL"
                    else f"Opening {issue.id} because it is assigned to {issue.assignee}."
                )
                intent_trace.confidence = 0.86
            return

        if validated_action and validated_action.type == "HIGHLIGHT_CREATE_TICKET_BUTTON":
            intent_trace.goal = "Create issue"
            intent_trace.current_intent = "Find create ticket entry point"
            intent_trace.relevant_feature = "Issues"
            intent_trace.reason = "Showing where ticket creation starts in the Issues view."
            intent_trace.confidence = 0.86
            return

        if validated_action and validated_action.type == "HIGHLIGHT_ADD_MEMBER_BUTTON":
            name = validated_action.payload.get("name", "requested assignee")
            intent_trace.goal = "Validate assignee"
            intent_trace.current_intent = "Add missing team member"
            intent_trace.relevant_feature = "Teams"
            intent_trace.reason = f"{name} must be added to the team directory before assigning work."
            intent_trace.confidence = 0.88
            return

        if validated_action and validated_action.type in {
            "OPEN_GITHUB_SETUP",
            "HIGHLIGHT_GITHUB_CARD",
            "HIGHLIGHT_SLACK_CARD",
        }:
            intent_trace.goal = "Connected workflow"
            intent_trace.current_intent = (
                "Configure integration"
                if validated_action.type == "OPEN_GITHUB_SETUP"
                else "Inspect integration"
            )
            intent_trace.relevant_feature = "Integrations"
            intent_trace.reason = "Showing the requested integration workflow inside the controlled demo."
            intent_trace.confidence = 0.86
            return

        if validated_action and validated_action.type == "FILTER_ISSUES_BY_ASSIGNEE":
            assignee = validated_action.payload.get("assignee")
            if isinstance(assignee, str):
                intent_trace.goal = "Issue lookup"
                intent_trace.current_intent = "Filter issues"
                intent_trace.relevant_feature = "Issues"
                intent_trace.reason = f"Filtering issues assigned to {assignee}."
                intent_trace.confidence = 0.86
            return

        unknown_person = extract_unknown_person(message)
        if unknown_person and self._mentions_ticket(message):
            intent_trace.goal = "Issue lookup"
            intent_trace.current_intent = "Open specific issue"
            intent_trace.relevant_feature = "Issues"
            intent_trace.reason = (
                f"No demo ticket matched {unknown_person}, so the agent is showing the Issues list."
            )
            intent_trace.confidence = 0.64

    def _mentions_ticket(self, message: str) -> bool:
        text = message.lower()
        return any(term in text for term in ("ticket", "issue", "bug"))

    def _grounded_prefix(self, retrieved_docs: list[RetrievedDocument]) -> str:
        if not retrieved_docs:
            return ""
        return self._format_sentence(retrieved_docs[0].snippet.split(". ", 1)[0])

    def _grounded_sentence(
        self,
        retrieved_docs: list[RetrievedDocument],
        keywords: tuple[str, ...],
    ) -> str:
        for document in retrieved_docs:
            for sentence in document.snippet.split(". "):
                normalized = sentence.lower()
                if any(keyword in normalized for keyword in keywords):
                    return self._format_sentence(sentence)
        return self._grounded_prefix(retrieved_docs)

    def _format_sentence(self, sentence: str) -> str:
        formatted = sentence.strip()
        formatted = formatted.split(", where", 1)[0].strip()
        if not formatted:
            return ""
        if not formatted.endswith("."):
            formatted = f"{formatted}."
        return f"{formatted} "

    def _context_payload(self, retrieved_docs: list[RetrievedDocument]) -> list[RetrievedContext]:
        return [
            RetrievedContext(
                title=document.title,
                source=document.source,
                snippet=document.snippet,
            )
            for document in retrieved_docs
        ]

    def _denied_response(self, request: TurnRequest, speech: str, reason: str) -> TurnResponse:
        """Every caller returns before the turn is activated, so the conversation is unchanged."""
        response = TurnResponse(
            session_id=request.session_id,
            turn_id=request.turn_id,
            status="denied",
            speech=speech,
            proposed_action=None,
            validated_action=None,
            intent_trace=IntentTrace(status="denied", reason=reason),
            signals=[],
            retrieved_context=[],
        )
        response._engine_entered = False
        return response

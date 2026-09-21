from __future__ import annotations

import re

from app.auth import AuthUser
from app.definitions.access import AccessDenied, ProductAccess, authorize_product
from app.definitions.organizations import OrganizationDirectory
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
        if not self.sessions.activate_turn(request.session_id, request.turn_id):
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

    def cancel_turn(self, session_id: str, turn_id: int):
        return self.sessions.cancel_turn(session_id, turn_id)

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
        elif self._asks_incomplete_ticket_create(normalized_message):
            speech = "Who should own this ticket? Name a teammate in this workspace and I'll prepare the form."
            reason = "The visitor asked to create a ticket but did not specify an assignee."
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
        return bool(
            re.search(r"\b(create|make|add|raise|file)\b", text)
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

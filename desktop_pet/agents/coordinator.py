"""Host-owned serial admission, authorization fences and durable queue proof.

The coordinator does not call a model or create a worker. A single trusted host
callback reuses ChatController's existing saved-turn execution path. Remote and
model dictionaries cannot mint run contexts, scope checks or closure receipts.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, is_dataclass
import hashlib
import json
import re
import threading
import time
from uuid import UUID, uuid5, NAMESPACE_URL

from .contracts import AgentError
from .job_queue import (HostDispatchScope, HostJobReference, JobQueueError,
                        SavedReplyProof, WorkerStoppedProof)

_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ORIGIN_FIELDS = {"version", "peer_id", "request_id", "authorization_scope", "settings_scope"}


class CoordinatorError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _uuid(value):
    try: return isinstance(value,str) and str(UUID(value)) == value and UUID(value).int != 0
    except (ValueError,TypeError,AttributeError): return False


def operation_identity(peer, request):
    if not isinstance(peer,str) or not _HASH.fullmatch(peer) or not _uuid(request):
        raise CoordinatorError("QUEUE_ORIGIN_INVALID")
    return str(uuid5(NAMESPACE_URL,"aura-remote:"+peer+":"+request))


def settings_identity(configuration, *, peer, authorization, generation):
    def public(value):
        if is_dataclass(value): return asdict(value)
        if isinstance(value,dict): return dict(value)
        raise CoordinatorError("QUEUE_CONFIGURATION_INVALID")
    try:
        key, gui_key = configuration["key"], configuration.get("gui_key","")
        if not isinstance(key,str) or not key or not isinstance(gui_key,str):
            raise CoordinatorError("QUEUE_CONFIGURATION_INVALID")
        source={"version":1,"api":public(configuration["api"]),"agent":public(configuration["agent"]),
                "credentials":[hashlib.sha256(value.encode()).hexdigest() for value in (key,gui_key)],
                "peer":peer,"authorization":authorization,"generation":generation}
        office_identity=configuration.get("office_renderer_identity", "")
        if not isinstance(office_identity,str) or office_identity and not _HASH.fullmatch(office_identity):
            raise CoordinatorError("QUEUE_CONFIGURATION_INVALID")
        if office_identity:
            source["office_renderer_identity"]=office_identity
        execution_identity=configuration.get("project_execution_identity", "")
        if not isinstance(execution_identity,str) or execution_identity and not _HASH.fullmatch(execution_identity):
            raise CoordinatorError("QUEUE_CONFIGURATION_INVALID")
        if execution_identity:
            source["project_execution_identity"]=execution_identity
        return hashlib.sha256(json.dumps(source,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
    except CoordinatorError: raise
    except Exception: raise CoordinatorError("QUEUE_CONFIGURATION_INVALID") from None


@dataclass(eq=False)
class _QueuedRun:
    claim: object
    request: dict | None = None
    handle: object = None
    attempt: object = None
    finished: bool = False
    result: dict | None = None
    last_heartbeat: float = 0
    executing_service: bool = False


def _completion_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class _UnknownCompletion:
    job: object
    request: dict
    token: int
    attempt: object
    attempt_owner: object
    handle: object
    user_digest: str
    reply_digest: str
    emotion: str
    execution_digest: str
    origin_digest: str
    artifact_refs: tuple


class HostAgentCoordinator:
    def __init__(self, queue, store, service, *, configuration, invalidated, start, idle, wake=lambda:None,
                 allow_new=lambda cid:False, completion_invalidated=None):
        self.queue, self.store, self.service = queue,store,service
        self.configuration,self.invalidated,self.start,self.idle,self.wake=configuration,invalidated,start,idle,wake
        self.completion_invalidated = invalidated if completion_invalidated is None else completion_invalidated
        self.allow_new=allow_new
        self.lock=threading.RLock()
        self._authority=None
        self._active=None
        self._closed=False
        self._ready=False
        self._failed=False
        self._recovery_polls_left=0

    def initialize(self):
        with self.lock:
            try:
                self.queue.initialize()
                self.queue.recover_after_restart()
                self._recovery_polls_left=3
                self._recover_guardians()
            except Exception:
                self._failed=True
                return False
            self._ready=True
            return True

    def _recover_guardians(self):
        if self._recovery_polls_left <= 0:
            return
        try:
            if not self.queue.unclosed_guardian_bindings(limit=1):
                self._recovery_polls_left=0
                return
            self._recovery_polls_left-=1
            recover=getattr(self.service,"recover_guardian_jobs",None)
            if callable(recover): recover(self.queue)
            if not self.queue.unclosed_guardian_bindings(limit=1): self._recovery_polls_left=0
        except Exception:
            # No proof means the existing unknown barrier remains. This is a
            # bounded read/reconciliation poll, never a worker/model retry.
            self._recovery_polls_left=0

    @property
    def recovery_pending(self):
        with self.lock:
            return (not self._closed and not self._failed
                and (self._recovery_polls_left > 0 or self._service_resources_pending()))

    def bind_authority(self, provider):
        if not callable(provider): raise CoordinatorError("QUEUE_AUTHORITY_REQUIRED")
        with self.lock: self._authority=provider

    def _authorization(self, peer, *, create=False):
        if self._authority is None: return None
        value=self._authority(peer,create=create)
        return value if isinstance(value,str) and _HASH.fullmatch(value) else None

    def _generation(self,cid):
        if self.invalidated(cid): raise CoordinatorError("QUEUE_CONVERSATION_INVALID")
        return self.store.read_conversation_generation(cid)

    @staticmethod
    def _origin(message):
        value=(message.get("metadata") or {}).get("agent_queue_origin")
        if (message.get("role") != "user" or message.get("source") != "user"
                or message.get("metadata",{}).get("remote_import") is True
                or not isinstance(value,dict) or set(value)!=_ORIGIN_FIELDS
                or type(value.get("version")) is not int or value["version"]!=1):
            raise CoordinatorError("QUEUE_ORIGIN_INVALID")
        return value

    def admit(self, peer, request_id, cid, text, *, image_id=None):
        with self.lock:
            if self._closed or self._failed or not self._ready: raise CoordinatorError("QUEUE_UNAVAILABLE")
            identity=operation_identity(peer,request_id)
            if not _uuid(cid) or not isinstance(text,str) or not text.strip() or len(text)>8000:
                raise CoordinatorError("QUEUE_REQUEST_INVALID")
            missing=self.store.get_conversation(cid) is None
            if missing and not self.allow_new(cid): raise CoordinatorError("QUEUE_CONVERSATION_INVALID")
            authorization=self._authorization(peer,create=True)
            if authorization is None: raise CoordinatorError("QUEUE_AUTHORIZATION_REVOKED")
            configuration=self.configuration()
            if configuration is None or not configuration["agent"].enabled: raise CoordinatorError("QUEUE_AGENT_DISABLED")
            generation=self._generation(cid) or identity
            existing=self.store.read_message(cid,identity)
            scope=settings_identity(configuration,peer=peer,authorization=authorization,generation=generation)
            if existing is not None:
                origin=self._origin(existing)
                if (origin["peer_id"]!=peer or origin["request_id"]!=request_id
                        or origin["authorization_scope"]!=authorization or existing["content"]!=text
                        or existing.get("metadata",{}).get("image_id")!=image_id):
                    raise CoordinatorError("QUEUE_REQUEST_CONFLICT")
                # A saved enrollment retains its original settings snapshot.
                # New settings cannot silently re-authorize an old request.
                scope=origin["settings_scope"]
            if missing:
                # Fresh authenticated admission may create a phone's new CID.
                # Existing append atomically creates its genuine user message;
                # durable tombstone triggers still prohibit resurrection.
                metadata={"request_status":"queued","execution_mode":"agent","agent_queue_origin":{
                    "version":1,"peer_id":peer,"request_id":request_id,
                    "authorization_scope":authorization,"settings_scope":scope}}
                if image_id is not None: metadata["image_id"]=image_id
                self.store.append_message(cid,"user",text,"user",message_id=identity,metadata=metadata)
            message=self.store.save_queued_agent_message(cid,identity,text,origin_peer_id=peer,
                operation_id=request_id,authorization_scope=authorization,settings_scope=scope,image_id=image_id)
            try:
                job=self.queue.enqueue(HostJobReference(cid,generation,message["id"],identity,scope,peer))
            except Exception:
                # Read back this exact enrollment after an ambiguous COMMIT;
                # never create a second identity or rerun an old message.
                try: job=self.queue.lookup(identity)
                except Exception:
                    self._failed=True
                    raise CoordinatorError("QUEUE_ENROLLMENT_UNCONFIRMED") from None
                if job is None:
                    self.store.set_message_status(cid,message["id"],"failed",agent_error_code="checkpoint_unavailable")
                    raise CoordinatorError("QUEUE_ENROLLMENT_FAILED") from None
            self._recovery_polls_left=max(self._recovery_polls_left,3)
        self.wake()
        return self.ack(job)

    @staticmethod
    def ack(job):
        return {"accepted":True,"conversation_id":job.conversation_id,"message_id":job.user_message_id,
                "job_id":job.job_id,"queue_state":job.state,"execution_mode":"agent","reply_delivery":"sync.pull"}

    def recover_ack(self, peer, request_id):
        with self.lock:
            if not self._ready or self._closed or self._authorization(peer) is None: return None
            job=self.queue.lookup(operation_identity(peer,request_id))
            if job is None or job.origin_peer_id != peer: return None
            message=self.store.read_message(job.conversation_id,job.user_message_id)
            if message is None: return None
            origin=self._origin(message)
            if (origin["peer_id"]!=peer or origin["request_id"]!=request_id
                    or origin["authorization_scope"]!=self._authorization(peer)): return None
            return self.ack(job)

    def _scope(self, job, *, reserved=False, allow_pending=False):
        try:
            message=self.store.read_message(job.conversation_id,job.user_message_id)
            if message is None: raise CoordinatorError("QUEUE_MESSAGE_MISSING")
            origin=self._origin(message)
            authorization=self._authorization(job.origin_peer_id)
            generation=self._generation(job.conversation_id)
            configuration=self.configuration()
            allowed=(not self._closed and not self._failed and configuration["agent"].enabled
                and authorization is not None and origin["peer_id"]==job.origin_peer_id
                and origin["authorization_scope"]==authorization and origin["settings_scope"]==job.settings_scope
                and operation_identity(job.origin_peer_id,origin["request_id"])==job.idempotency_key
                and message["metadata"].get("execution_mode")=="agent"
                and message["metadata"].get("request_status") in ({"queued","pending"} if reserved or allow_pending else {"queued"})
                and (reserved or message["metadata"].get("agent_execution_started") is not True))
            scope=settings_identity(configuration,peer=job.origin_peer_id,authorization=authorization,generation=generation)
            return HostDispatchScope(job.conversation_id,generation,scope,allowed,job.origin_peer_id)
        except Exception:
            return HostDispatchScope(job.conversation_id,job.generation,job.settings_scope,False,job.origin_peer_id)

    def effect_barrier(self):
        with self.lock:
            return self._failed or self._closed or not self._ready or self.queue.has_unclosed_worker() or self._guardian_resources_pending()

    def _guardian_resources_pending(self):
        method=getattr(self.service,"guardian_resources_pending",None)
        if not callable(method): return False
        try: return method() is True
        except Exception: return True

    def _service_resources_pending(self):
        query = getattr(self.service, "execution_resources_pending", None)
        if not callable(query):
            return False
        try:
            pending = query()
            return pending if type(pending) is bool else True
        except Exception:
            return True

    def pump(self):
        with self.lock:
            if self._closed or self._failed or not self._ready: return
            if self._active is not None:
                context=self._active
                if context.finished:
                    self._finish(context)
                    if self._active is None: self.wake()
                elif time.monotonic()-context.last_heartbeat>=5:
                    try:
                        self.queue.heartbeat(context.claim.lease,ttl_seconds=30)
                        context.last_heartbeat=time.monotonic()
                    except JobQueueError: pass
                return
            self._recover_guardians()
            if not self.idle(): return
            if self._service_resources_pending():
                # A finished local request has no queued _active owner. Keep
                # queued identities intact until its exact worker is closed.
                # The existing timer repeats only this bounded host cleanup.
                try: self.service.reconcile_worker_closures(timeout_seconds=0)
                except Exception: pass
                if self._service_resources_pending(): return
            if self.queue.has_unclosed_worker() or self._guardian_resources_pending(): return
            for _ in range(16):
                claim=self.queue.claim(ttl_seconds=30)
                if claim is None: return
                decision=self.queue.begin_dispatch(claim.lease,revalidate=self._scope)
                if not decision.accepted:
                    try:
                        self.store.set_message_status(claim.job.conversation_id,claim.job.user_message_id,"interrupted",
                                                      agent_error_code="cancelled")
                    except Exception: pass
                    continue
                message=self.store.read_message(claim.job.conversation_id,claim.job.user_message_id)
                context=_QueuedRun(claim,last_heartbeat=time.monotonic())
                self._active=context
                try: self.start(context,message,self.configuration())
                except Exception:
                    context.finished=True
                    self._finish(context)
                return
            self.wake()

    def attach_request(self, context, request):
        with self.lock:
            if context is not self._active or type(context) is not _QueuedRun or context.request is not None:
                raise CoordinatorError("QUEUE_RUN_IDENTITY_INVALID")
            if (request["conversation_id"],request["message_id"]) != (context.claim.job.conversation_id,context.claim.job.user_message_id):
                raise CoordinatorError("QUEUE_RUN_IDENTITY_INVALID")
            context.request=request

    def _guard(self, request, context, *, reserved, prepared=None):
        policy=self.configuration()
        if (self._closed or self._failed or not self._ready or request["cancel"].is_set()
                or policy is None or not policy["agent"].enabled or self.queue.has_unclosed_worker() or self._guardian_resources_pending()):
            raise AgentError("busy","已有电脑任务或授权状态尚未确认，本轮未继续派发。")
        if context is None:
            if self._active is not None: raise AgentError("busy","已有电脑任务正在收束，本轮未继续派发。")
            if self.invalidated(request["conversation_id"]): raise AgentError("cancelled","会话已变更，本轮已停止。")
            try:
                generation=self._generation(request["conversation_id"])
                current=settings_identity(policy,peer=None,authorization=None,generation=generation)
                actual=settings_identity(prepared,peer=None,authorization=None,generation=generation) if prepared is not None else current
            except Exception:
                raise AgentError("busy","任务设置暂不可确认，本轮未继续派发。") from None
            if not reserved:
                if current!=actual: raise AgentError("cancelled","任务设置已改变，本轮已停止。")
                request["queue_policy_identity"]=actual
            elif request.get("queue_policy_identity")!=current:
                raise AgentError("cancelled","任务设置已改变，本轮已停止。")
        else:
            if context is not self._active or context.request is not request:
                raise AgentError("cancelled","任务身份已失效，本轮已停止。")
            scope=self._scope(context.claim.job,reserved=reserved,allow_pending=True)
            if (not scope.authorized or scope.generation!=context.claim.job.generation
                    or scope.settings_scope!=context.claim.job.settings_scope):
                raise AgentError("cancelled","任务授权或设置已改变，本轮已停止。")
            if prepared is not None:
                authorization=self._authorization(context.claim.job.origin_peer_id)
                actual=settings_identity(prepared,peer=context.claim.job.origin_peer_id,authorization=authorization,
                                         generation=context.claim.job.generation)
                if actual!=context.claim.job.settings_scope:
                    raise AgentError("cancelled","任务设置已改变，本轮已停止。")
            try:
                # Validate the exact lease at every executable boundary. A
                # stalled Qt heartbeat cannot leave an expired owner running
                # more effects merely because recovery has not scanned it yet.
                self.queue.heartbeat(context.claim.lease,ttl_seconds=30)
                context.last_heartbeat=time.monotonic()
            except JobQueueError:
                raise AgentError("busy","任务执行租约已失效，本轮已停止。") from None

    @contextmanager
    def reserve_guard(self, request, context=None, *, prepared=None):
        with self.lock:
            self._guard(request,context,reserved=False,prepared=prepared)
            yield

    def worker_bound(self, request, context, handle):
        with self.lock:
            request["worker_handle"]=handle
            if context is not None:
                if context is not self._active or context.request is not request:
                    raise AgentError("cancelled","任务身份已失效，本轮已停止。")
                if context.handle is not None and context.handle is not handle:
                    raise AgentError("cancelled","后台身份已改变，本轮已停止。")
                context.handle=handle

    def guardian_launch(self, request, context):
        """Remote-queued only. No local message/metadata can mint this object."""
        if context is None:
            return None  # Direct desktop deliberately keeps the Popen backend.
        from .guardian_launch import _issue
        with self.lock:
            self._guard(request, context, reserved=True)
            job = self.queue.get(context.claim.job.job_id)
            if job.origin_peer_id is None:
                raise AgentError("cancelled", "任务缺少可核验的远程来源，本轮未启动。")
            reference = HostJobReference(job.conversation_id, job.generation, job.user_message_id,
                                         job.idempotency_key, job.settings_scope, job.origin_peer_id)
            def attempt(current):
                with self.lock:
                    self._guard(request, context, reserved=True)
                    if (context.attempt is not current or request.get("execution_attempt") is not current
                            or not self.service.validate_execution_attempt(current)):
                        raise AgentError("cancelled", "任务执行身份已失效，本轮未启动。")
                    return True
            def commit(manifest):
                with self.lock:
                    self._guard(request, context, reserved=True)
                    try:
                        binding = self.queue.bind_guardian_run(context.claim.lease, manifest)
                    except JobQueueError:
                        # A commit can have happened before its acknowledgement
                        # failed. Read back the SAME run; never create another.
                        binding = self.queue.guardian_binding(job.job_id)
                        if binding is None or not binding.matches_manifest(manifest):
                            raise AgentError("checkpoint_unavailable", "任务运行身份未能确认，本轮未继续启动。") from None
                    self._guard(request, context, reserved=True)
                    return binding
            return _issue(job.job_id, reference, commit, attempt)

    def execution_attempt_bound(self, request, context, attempt):
        with self.lock:
            if (not self.service.validate_execution_attempt(attempt)
                    or attempt.conversation_id != request["conversation_id"]
                    or request.get("execution_attempt") is not None):
                raise AgentError("cancelled","任务执行身份已失效，本轮已停止。")
            if context is not None:
                if (context is not self._active or context.request is not request
                        or context.attempt is not None):
                    raise AgentError("cancelled","任务执行身份已失效，本轮已停止。")
            owner=object()
            if not self.service.bind_execution_attempt(attempt,owner):
                raise AgentError("cancelled","任务执行身份已被占用，本轮已停止。")
            if context is not None: context.attempt=attempt
            request["execution_attempt"]=attempt
            request["execution_attempt_owner"]=owner

    @contextmanager
    def dispatch_guard(self, request, context, handle):
        with self.lock:
            if request.get("worker_handle") is not handle:
                raise AgentError("cancelled","后台身份已失效，本轮已停止。")
            self._guard(request,context,reserved=True)
            yield

    def service_entered(self, context):
        if context is not None:
            with self.lock:
                if context is self._active: context.executing_service=True

    def interaction(self,context,request,pending):
        if context is None: return
        with self.lock:
            if context is not self._active or context.request is not request or context.finished: return
            try:
                job=self.queue.get(context.claim.job.job_id)
                if pending is not None and job.state=="running":
                    self.queue.set_awaiting_user(context.claim.lease,awaiting=True)
                elif pending is None and job.state=="awaiting_user":
                    self.queue.set_awaiting_user(context.claim.lease,awaiting=False)
            except JobQueueError: pass  # This display state grants no effects.

    def finished(self, context, request, result):
        with self.lock:
            if context is not self._active or context.request is not request: return
            context.finished,context.result=True,result
            self._finish(context)
        self.wake()

    def _closed_proof(self, context):
        if not context.executing_service: return True  # This saved path never invoked a worker.
        if context.handle is None:
            # Only this invocation's finished no-launch receipt can release
            # its slot. A missing worker callback/empty registry is no proof.
            try:
                receipt=self.service.execution_no_launch(context.attempt)
                return (context.finished and context.request is not None
                    and context.request.get("execution_attempt") is context.attempt
                    and receipt is not None and receipt.conversation_id==context.claim.job.conversation_id
                    and type(context.request.get("execution_attempt_owner")) is object
                    and self.service.validate_execution_no_launch(context.attempt,receipt,
                        owner=context.request["execution_attempt_owner"]))
            except Exception: return False
        try:
            receipt=self.service.worker_closure(context.handle)
            return (receipt is not None and receipt.conversation_id==context.claim.job.conversation_id
                    and self.service.validate_worker_closure(context.handle,receipt))
        except Exception: return False

    def _historical_completion_proof(self, context, current, reply_id):
        """Re-read an expired run's saved outcome, without renewing its lease."""
        request = context.request
        claimed = context.claim.job
        if (context is not self._active or context.finished is not True
                or not context.executing_service or context.handle is None or request is None
                or request.get("committed") is not True
                or request.get("worker_handle") is not context.handle
                or request.get("result_state") != "completed" or request.get("saved_reply_id") != reply_id
                or request.get("conversation_id") != current.conversation_id
                or request.get("message_id") != current.user_message_id
                or current.state not in {"unknown", "interrupted"} or current.reason_code != "LEASE_EXPIRED"
                or current.lease_expires_at is not None or current.finished_at is None
                or claimed.lease_expires_at is None or current.finished_at < claimed.lease_expires_at):
            return None
        identity = ("job_id", "conversation_id", "generation", "user_message_id",
                    "idempotency_key", "settings_scope", "origin_peer_id")
        if any(getattr(current, name) != getattr(claimed, name) for name in identity):
            return None
        if not self._closed_proof(context) or self.completion_invalidated(current.conversation_id):
            return None
        reply = self.store.read_message(current.conversation_id, reply_id)
        message = self.store.read_message(current.conversation_id, current.user_message_id)
        if (reply is None or reply["role"] != "assistant" or reply["source"] != "assistant"
                or reply.get("metadata", {}).get("reply_to") != current.user_message_id
                or message is None or message["role"] != "user" or message["source"] != "user"
                or message.get("metadata", {}).get("request_status") != "complete"
                or message.get("metadata", {}).get("agent_execution_started") is not True
                or self.store.read_conversation_generation(current.conversation_id) != current.generation):
            return None
        origin = self._origin(message)
        if (origin["peer_id"] != current.origin_peer_id or origin["settings_scope"] != current.settings_scope
                or operation_identity(origin["peer_id"], origin["request_id"]) != current.idempotency_key):
            return None
        references = tuple(request.get("agent_artifact_refs", ()))
        if references:
            available = {item["reference"] for item in self.service.artifacts(current.conversation_id)}
            if not set(references) <= available:
                return None
        return SavedReplyProof(current.job_id, current.conversation_id, current.generation,
            current.user_message_id, reply_id, True, True, references)

    @staticmethod
    def unknown_completion_candidate(context, request, reply, execution, generation, *, completed):
        """Freeze original expectations without acquiring a lock or reading storage.

        Called only while the original save/Stop commit lock is held. Keeping
        the retired context itself would retain worker callbacks and leases.
        """
        if (completed is not True or type(context) is not _QueuedRun or context.request is not request
                or context.finished or not context.executing_service or context.handle is None
                or context.attempt is None or request.get("worker_handle") is not context.handle
                or request.get("execution_attempt") is not context.attempt
                or type(request.get("execution_attempt_owner")) is not object
                or request["cancel"].is_set() or request.get("result_state") != "completed"
                or execution.get("state") != "completed"
                or generation != context.claim.job.generation
                or (request["conversation_id"], request["message_id"]) !=
                   (context.claim.job.conversation_id, context.claim.job.user_message_id)):
            return None
        return _UnknownCompletion(context.claim.job, request, request["token"], context.attempt,
            request["execution_attempt_owner"], context.handle, _completion_digest(request["content"]),
            _completion_digest(reply.reply), reply.emotion, _completion_digest(execution),
            _completion_digest(request["metadata"].get("agent_queue_origin")),
            tuple(request.get("agent_artifact_refs", ())))

    def _unknown_completion_proof(self, candidate, current, reply_id, accepted_read):
        request, claimed = candidate.request, candidate.job
        identity = ("job_id", "conversation_id", "generation", "user_message_id",
                    "idempotency_key", "settings_scope", "origin_peer_id")
        interrupted = current.state == "interrupted" and current.reason_code == "WORKER_INTERRUPTED"
        confirmed = (current.state == "completed" and current.reason_code == "SAVED_REPLY_CONFIRMED"
                     and current.reply_id == reply_id)
        if (not accepted_read() or self._closed or not (interrupted or confirmed)
                or current.lease_expires_at is not None or current.worker_closed_at is None
                or current.finished_at is None
                or any(getattr(current, name) != getattr(claimed, name) for name in identity)
                or request.get("token") != candidate.token or request["cancel"].is_set()
                or (request.get("conversation_id"), request.get("message_id")) !=
                   (current.conversation_id, current.user_message_id)
                or _completion_digest(request.get("content")) != candidate.user_digest
                or request.get("result_state") != "completed"
                or request.get("execution_attempt") is not candidate.attempt
                or request.get("execution_attempt_owner") is not candidate.attempt_owner
                or request.get("worker_handle") is not candidate.handle
                or tuple(request.get("agent_artifact_refs", ())) != candidate.artifact_refs
                or self.completion_invalidated(current.conversation_id)):
            return None
        receipt = self.service.worker_closure(candidate.handle)
        if (receipt is None or receipt.conversation_id != current.conversation_id
                or not self.service.validate_worker_closure(candidate.handle, receipt)):
            return None
        messages = self.store.load_messages(current.conversation_id)
        if not messages or messages[0]["id"] != current.generation:
            return None
        user = next((item for item in messages if item["id"] == current.user_message_id), None)
        replies = [item for item in messages if item.get("metadata", {}).get("reply_to") == current.user_message_id]
        if user is None or len(replies) != 1:
            return None
        reply = replies[0]
        user_metadata, reply_metadata = user.get("metadata", {}), reply.get("metadata", {})
        if (user.get("conversation_id") != current.conversation_id or user.get("role") != "user"
                or user.get("source") != "user" or _completion_digest(user.get("content")) != candidate.user_digest
                or user_metadata.get("request_status") != "complete"
                or user_metadata.get("agent_execution_started") is not True
                or user_metadata.get("execution_mode") != "agent"
                or _completion_digest(user_metadata.get("execution")) != candidate.execution_digest
                or reply.get("conversation_id") != current.conversation_id or reply.get("id") != reply_id
                or reply.get("role") != "assistant" or reply.get("source") != "assistant"
                or _completion_digest(reply.get("content")) != candidate.reply_digest
                or reply_metadata.get("emotion") != candidate.emotion
                or _completion_digest(reply_metadata.get("execution")) != candidate.execution_digest):
            return None
        origin = self._origin(user)
        if (_completion_digest(origin) != candidate.origin_digest or origin["peer_id"] != current.origin_peer_id
                or origin["settings_scope"] != current.settings_scope
                or operation_identity(origin["peer_id"], origin["request_id"]) != current.idempotency_key):
            return None
        if candidate.artifact_refs:
            available = {item["reference"] for item in self.service.artifacts(current.conversation_id)}
            if not set(candidate.artifact_refs) <= available:
                return None
        if (not accepted_read() or self.completion_invalidated(current.conversation_id)
                or not self.service.validate_worker_closure(candidate.handle, receipt)):
            return None
        return SavedReplyProof(current.job_id, current.conversation_id, current.generation,
            current.user_message_id, reply_id, True, True, candidate.artifact_refs)

    def reconcile_unknown_completion(self, candidate, messages, *, accepted_read):
        """Settle an accepted explicit history read, without reviving a run."""
        if type(candidate) is not _UnknownCompletion:
            return None
        with self.lock:
            if not accepted_read():
                return None
            replies = [item for item in messages
                       if item.get("metadata", {}).get("reply_to") == candidate.job.user_message_id]
            if len(replies) != 1 or not isinstance(replies[0].get("id"), str):
                return None
            reply_id = replies[0]["id"]
            current = self.queue.get(candidate.job.job_id)
            if current.state == "completed":
                # The ledger COMMIT may also have lost its acknowledgement.
                # Queue's idempotent branch skips its verifier, so verify here.
                proof = self._unknown_completion_proof(candidate, current, reply_id, accepted_read)
                return reply_id if proof is not None else None
            self.queue.reconcile_completed(current.job_id, reply_id, lease=None,
                verifier=lambda latest, saved:self._unknown_completion_proof(candidate, latest, saved, accepted_read))
            return reply_id

    def _finish(self, context):
        job=context.claim.job
        if not self._closed_proof(context):
            try: self.service.reconcile_worker_closures(timeout_seconds=0)
            except Exception: pass
            if not self._closed_proof(context):
                self.queue.recover_expired()
                return
        snapshot=self.queue.get(job.job_id)
        result=context.result or {}
        complete=(context.handle is not None and context.request is not None and context.request.get("result_state")=="completed"
                  and isinstance(context.request.get("saved_reply_id"),str))
        if complete:
            reply_id=context.request["saved_reply_id"]
            reply=self.store.read_message(job.conversation_id,reply_id)
            message=self.store.read_message(job.conversation_id,job.user_message_id)
            complete=(reply is not None and reply["role"]=="assistant" and reply["source"]=="assistant"
                and reply.get("metadata",{}).get("reply_to")==job.user_message_id and message is not None
                and message.get("metadata",{}).get("agent_execution_started") is True
                and not self.completion_invalidated(job.conversation_id)
                and self.store.read_conversation_generation(job.conversation_id)==job.generation)
            references=tuple(context.request.get("agent_artifact_refs",()))
            if references:
                try:
                    available={item["reference"] for item in self.service.artifacts(job.conversation_id)}
                    complete=complete and set(references)<=available
                except Exception: complete=False
        try:
            if complete:
                proof=SavedReplyProof(job.job_id,job.conversation_id,job.generation,job.user_message_id,reply_id,True,True,references)
                self.queue.reconcile_completed(job.job_id,reply_id,lease=context.claim.lease,
                    verifier=lambda *_:proof)
            else:
                cancelled=context.request is not None and context.request["cancel"].is_set()
                self.queue.host_acknowledge_stopped(context.claim.lease,
                    outcome="cancelled" if cancelled else "interrupted",worker_stopped=True)
        except JobQueueError as error:
            self.queue.recover_expired()
            current=self.queue.get(job.job_id)
            if (complete and str(error) in {"QUEUE_LEASE_EXPIRED", "QUEUE_LEASE_MISMATCH"}
                    and current.state in {"unknown", "interrupted"} and current.reason_code == "LEASE_EXPIRED"
                    and current.lease_expires_at is None and context.request.get("committed") is True):
                # This transaction settles history only. The verifier observes
                # current archive/closure facts under the ledger's write lock.
                self.queue.reconcile_completed(job.job_id, reply_id, lease=None,
                    verifier=lambda latest, saved:self._historical_completion_proof(context, latest, saved))
            elif current.state in {"unknown","interrupted","cancel_requested"}:
                proof=WorkerStoppedProof(job.job_id,job.conversation_id,job.generation,job.user_message_id,True)
                self.queue.reconcile_worker_stopped(job.job_id,verifier=lambda *_:proof)
        self._active=None

    def cancel(self,cid,stop):
        with self.lock:
            context=self._active
            active_cancelled=False
            if context is not None and context.claim.job.conversation_id==cid:
                # The existing commit lock decides whether cancellation won.
                active_cancelled=bool(stop(cid))
                if active_cancelled: self.queue.request_cancel(context.claim.job.job_id)
            changed=self.queue.cancel_queued(cid)
            for job in changed:
                self.store.set_message_status(cid,job.user_message_id,"cancelled")
            return {"cancelled":active_cancelled or bool(changed),"active_cancelled":active_cancelled,
                    "queued_cancelled_count":len(changed)}

    def authority_changed(self, stop):
        with self.lock:
            context=self._active
            if context is not None and context.request is not None:
                scope=self._scope(context.claim.job,reserved=True)
                if not scope.authorized or scope.settings_scope!=context.claim.job.settings_scope:
                    stop(context.claim.job.conversation_id)
        self.wake()

    def invalidate(self,cid):
        with self.lock:
            # The new generation must stay usable after clear. Revoke only
            # generations actually recorded in this ledger, not the whole CID.
            generation=self.store.read_conversation_generation(cid)
            if generation is not None: self.queue.revoke_conversation(cid,generation=generation)
        self.wake()

    def shutdown(self):
        with self.lock: self._closed=True

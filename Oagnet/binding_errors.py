"""Typed dependency failures, distinct from missing business parameters.

No prompts, replies, credentials or raw exception messages enter public errors.
Retries remain owned by the existing capacity/HTTP boundaries.
"""
import json
import re


class BindingServiceError(RuntimeError):
    def __init__(self, code, *, stage, error_type, status_code=503, retryable=False):
        super().__init__('ASL binding dependency failed')
        self.code = code
        self.stage = stage
        self.error_type = error_type
        self.status_code = status_code
        self.retryable = retryable

    def public_detail(self):
        return {'code': self.code, 'message': str(self), 'retryable': self.retryable,
                'details': {'binding_stage': self.stage, 'error_type': self.error_type}}


def dependency_failure(exc, stage):
    """Inspect bounded exception causes without publishing arbitrary messages."""
    current, seen = exc, set()
    for _ in range(5):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        name = type(current).__name__
        status = getattr(current, 'status_code', None)
        if status is None:
            status = getattr(getattr(current, 'response', None), 'status_code', None)
        if status == 429 or name == 'RateLimitError':
            failure = BindingServiceError('ASL_BINDING_RATE_LIMITED', stage=stage,
                error_type=name, status_code=429, retryable=True)
            failure.response = getattr(current, 'response', None)
            return failure
        if status in (401, 403) or name in {'AuthenticationError', 'PermissionDeniedError'}:
            return BindingServiceError('ASL_BINDING_AUTH_FAILED', stage=stage,
                error_type=name, status_code=502)
        if isinstance(current, TimeoutError) or name in {
            'APITimeoutError', 'ReadTimeout', 'ConnectTimeout', 'WriteTimeout', 'PoolTimeout'}:
            return BindingServiceError('ASL_BINDING_TIMEOUT', stage=stage,
                error_type=name, status_code=504, retryable=True)
        if isinstance(current, ConnectionError) or name in {
            'APIConnectionError', 'ConnectError', 'NetworkError', 'RemoteProtocolError'}:
            return BindingServiceError('ASL_BINDING_UNAVAILABLE', stage=stage,
                error_type=name, retryable=True)
        if type(status) is int and status >= 500:
            return BindingServiceError('ASL_BINDING_UNAVAILABLE', stage=stage,
                error_type=name, retryable=True)
        if name in {'OperationalError', 'InterfaceError'} and current.args:
            number = current.args[0]
            if number in (1044, 1045):
                return BindingServiceError('ASL_BINDING_AUTH_FAILED', stage=stage,
                    error_type=name, status_code=502)
            if number in (0, 2002, 2003, 2006, 2013):
                return BindingServiceError('ASL_BINDING_UNAVAILABLE', stage=stage,
                    error_type=name, retryable=True)
        current = current.__cause__ or current.__context__
    return BindingServiceError('ASL_BINDING_FAILED', stage=stage, error_type=type(exc).__name__)


def invoke_binding_object(model, messages, *, stage):
    try:
        response = model.invoke(messages)
    except BindingServiceError:
        raise
    except Exception as exc:
        raise dependency_failure(exc, stage) from exc
    try:
        content = response.content
        if isinstance(content, list):
            if not content or any(not isinstance(b, dict) or b.get('type') != 'text'
                                  or not isinstance(b.get('text'), str) for b in content):
                raise ValueError('binding response must contain text only')
            content = ''.join(b['text'] for b in content)
        if not isinstance(content, str):
            raise TypeError('binding response must contain text')
        content = content.strip().lstrip('\ufeff').strip()
        fenced = re.fullmatch(r'```(?:json)?\s*(.*?)\s*```', content, re.S | re.I)
        plan = json.loads(fenced[1] if fenced else content)
        if not isinstance(plan, dict):
            raise ValueError('binding response must be an object')
        sections = ('metrics', 'dimensions', 'display_fields', 'filters', 'sort')
        recognized = {*sections, 'subject', 'subject_error', 'time'}
        if stage == 'relationship_binding':
            sections, recognized = ('bindings',), {'bindings', 'related_scope'}
            if 'related_scope' in plan and not isinstance(plan['related_scope'], dict):
                raise ValueError('relationship decision must be an object')
        # An omitted/null section is an existing recoverable binding shape:
        # exact scoped catalog matches may fill it without another model call.
        if not recognized.intersection(plan) or any(
            key in plan and plan[key] is not None and not isinstance(plan[key], list)
            for key in sections):
            raise ValueError('binding response has invalid sections')
        return plan
    except (ValueError, TypeError, AttributeError) as exc:
        raise BindingServiceError('ASL_BINDING_RESPONSE_INVALID', stage=stage,
            error_type=type(exc).__name__, status_code=502) from exc

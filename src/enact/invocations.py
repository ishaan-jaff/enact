# Copyright 2023 Agentic.AI Corporation.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Functionality for invokable resources."""

import contextlib
import dataclasses
import inspect
import traceback
from typing import (
  Any, Callable, Generic, Iterable, List, Mapping, Optional, Tuple, Type,
  TypeVar, cast)

from enact import contexts
from enact import interfaces
from enact import references
from enact import resources
from enact import resource_registry


C = TypeVar('C', bound=interfaces.ResourceBase)
E = TypeVar('E', bound='ExceptionResource')


@resource_registry.register
class ExceptionResource(interfaces.ResourceBase, Exception):
  """A resource that is also an exception."""

  def __init__(self, *args):
    Exception.__init__(self, *args)

  @classmethod
  def field_names(cls) -> Iterable[str]:
    """Returns the names of the fields of the resource."""
    yield 'args'

  def field_values(self) -> Iterable[interfaces.FieldValue]:
    """Return a list of field values, aligned with field_names."""
    yield self.args

  @classmethod
  def from_fields(cls: Type[C],
                  field_dict: Mapping[str, interfaces.FieldValue]) -> C:
    """Constructs the resource from a value dictionary."""
    return cls(*field_dict['args'])

  def set_from(self: C, other: Any):
    """Sets the fields of this resource from another resource."""
    super().set_from(other)  # Raise error


@resource_registry.register
class WrappedException(ExceptionResource):
  """A python exception wrapped as a resource."""


I_contra = TypeVar(
  'I_contra', contravariant=True, bound=interfaces.ResourceBase)
O_co = TypeVar('O_co', covariant=True, bound=interfaces.ResourceBase)


@resource_registry.register
class InputRequest(ExceptionResource):
  """An exception indicating that external input is required."""

  def __init__(
      self,
      invokable: references.Ref['_InvokableBase'],
      input_resource: references.Ref,
      requested_output: Type[interfaces.ResourceBase],
      context: interfaces.FieldValue):
    if not references.Store.get_current():
      raise contexts.NoActiveContext(
        'InputRequest must be created within a Store context.')
    super().__init__(
      invokable,
      input_resource,
      requested_output,
      context)

  @property
  def invokable(self) -> references.Ref['_InvokableBase']:
    """Returns the invokable that requested the input."""
    return self.args[0]

  @property
  def for_resource(self) -> references.Ref:
    """Returns a reference to the resource for which input is requested."""
    return self.args[1]

  @property
  def requested_type(self) -> Type[interfaces.ResourceBase]:
    """Returns the type of input requested."""
    return self.args[2]

  @property
  def context(self) -> interfaces.FieldValue:
    return self.args[3]

  def continue_invocation(
      self,
      invocation: 'Invocation[I_contra, O_co]',
      value: interfaces.ResourceBase,
      strict: bool=True) -> (
        'Invocation[I_contra, O_co]'):
    """Replays the invocation with the given value."""
    ref = references.commit(self)
    def _exception_override(exception_ref: references.Ref[ExceptionResource]):
      if exception_ref == ref:
        return value
      return None
    return invocation.replay(
      exception_override=_exception_override, strict=strict)

  async def continue_invocation_async(
      self,
      invocation: 'Invocation[I_contra, O_co]',
      value: interfaces.ResourceBase,
      strict: bool=True):
    ref = references.commit(self)
    def _exception_override(exception_ref: references.Ref[ExceptionResource]):
      if exception_ref == ref:
        return value
      return None
    return await invocation.replay_async(
      exception_override=_exception_override, strict=strict)


@resource_registry.register
class InputRequestOutsideInvocation(ExceptionResource):
  """Raised when input required is called outside an invocation."""


@resource_registry.register
class InvocationError(ExceptionResource):
  """An error during an invocation."""


@resource_registry.register
class InvokableTypeError(InvocationError, TypeError):
  """An type error on a callable input or output."""


@resource_registry.register
class InputChanged(InvocationError):
  """Raised when an input changes during invocation."""


@resource_registry.register
class RequestedTypeUndetermined(InvocationError):
  """Raised when the requested type cannot be determined."""


def _request_input(
    for_resource: Optional[interfaces.ResourceBase],
    requested_type: Optional[Type[interfaces.ResourceBase]],
    context: interfaces.FieldValue=None):
  """Requests an input from a user or external system.

  Args:
    for_resource: The resource for which input is requested.
    requested_type: The type of input requested. If not specified, the type will
      be inferred to be the output type of the current invocation.
    context: Anything that provides context for the request.
  Raises:
    InputRequest: The input request exception.
    InputRequestOutsideInvocation: If the request was made outside an
      invocation.
  """
  builder: Optional[Builder] = Builder.get_current()
  if not builder:
    raise InputRequestOutsideInvocation(context, requested_type)
  requested_type = requested_type or builder.invokable.get_output_type()
  if not requested_type:
    raise RequestedTypeUndetermined(
      'Requested type must be specified when output type is undetermined.')
  raise InputRequest(
    references.commit(builder.invokable),
    references.commit(for_resource),
    requested_type,
    context)


@resource_registry.register
@dataclasses.dataclass
class Request(Generic[I_contra, O_co], resources.Resource):
  """An invocation request."""
  invokable: references.Ref['_InvokableBase[I_contra, O_co]']
  input: references.Ref[I_contra]


@resource_registry.register
@dataclasses.dataclass
class Response(Generic[I_contra, O_co], resources.Resource):
  """An invocation response."""
  invokable: references.Ref['_InvokableBase[I_contra, O_co]']
  output: Optional[references.Ref[O_co]]
  # Exception raised during call.
  raised: Optional[references.Ref[ExceptionResource]]
  # Whether the exception was raised locally or propagated from a child.
  raised_here: bool
  # Subinvocations associated with this invocation.
  children: List[references.Ref['Invocation']]

  def is_complete(self) -> bool:
    """Returns whether this invocation is complete."""
    return self.raised is not None or self.output is not None


# A function that may override some exceptions that occur during invocation.
ExceptionOverride = Callable[[references.Ref[ExceptionResource]],
                             Optional[interfaces.ResourceBase]]


@resource_registry.register
@dataclasses.dataclass
class Invocation(Generic[I_contra, O_co], resources.Resource):
  """An invocation."""
  request: references.Ref[Request[I_contra, O_co]]
  response: references.Ref[Response[I_contra, O_co]]

  def successful(self) -> bool:
    """Returns true if the invocation completed successfully."""
    if not self.response:
      return False
    return self.response().output is not None

  def get_output(self) -> O_co:
    """Returns the output or raise assertion error."""
    output = self.response().output
    assert output
    return output()

  def get_raised(self) -> ExceptionResource:
    """Returns the raised exception or raise assertion error."""
    raised = self.response().raised
    assert raised
    return raised()

  def get_raised_here(self) -> bool:
    """Whether the exception was originally raised here or in a child."""
    response = self.response()
    assert response.raised, 'No exception was raised.'
    return response.raised_here

  def get_children(self) -> Iterable['Invocation']:
    """Yields the child invocations."""
    children = self.response().children
    for child in children:
      yield child()

  def get_child(self, index: int) -> 'Invocation':
    """Returns the child invocation corresponding to the index."""
    children = self.response().children
    return children[index]()

  def clear_output(self):
    """Clear the output of the invocation."""
    with self.response.modify() as response:
      response.output = None

  def rewind(self, num_calls=1) -> 'Invocation[I_contra, O_co]':
    """Rewinds the invocation by the specified number of calls."""
    invocation = self.deep_copy_resource()
    with invocation.response.modify() as response:
      response.output = None
      for _ in range(num_calls):
        if response.children:
          response.children.pop(-1)
    return invocation

  def replay(
      self,
      exception_override: ExceptionOverride=lambda x: None,
      strict: bool=True) -> (
        'Invocation[I_contra, O_co]'):
    """Replay the invocation, retrying exceptions or overiding them."""
    invokable = self.request().invokable()
    if isinstance(invokable, AsyncInvokableBase):
      raise InvocationError(
        'Cannot replay async invocations synchronously. '
        'Use the "replay_async" coroutine instead.')
    assert isinstance(invokable, InvokableBase)
    return invokable.invoke(
      self.request().input,
      replay_from=self,
      exception_override=exception_override,
      strict=strict)

  async def replay_async(
      self,
      exception_override: ExceptionOverride=lambda x: None,
      strict: bool=True) -> (
        'Invocation[I_contra, O_co]'):
    """Replay the invocation, retrying exceptions or overiding them."""
    invokable = self.request().invokable()
    if isinstance(invokable, InvokableBase):
      raise InvocationError(
        'Cannot replay synchronous invocations asynchronously. '
        'Use the "replay" function instead.')
    assert isinstance(invokable, AsyncInvokableBase)
    return await invokable.invoke(
      self.request().input,
      replay_from=self,
      exception_override=exception_override,
      strict=strict)


class ReplayError(InvocationError):
  """An error during replay."""


@contexts.register
class ReplayContext(Generic[I_contra, O_co], contexts.Context):
  """A replay of an invocation."""

  def __init__(
      self,
      subinvocations: Iterable[references.Ref[Invocation]],
      exception_override: ExceptionOverride=lambda x: None,
      strict: bool = True):
    """Create a new replay context.

    Args:
      subinvocations: The subinvocations to replay.
      exception_override: A function that may override exceptions that occur
        during replay. If an exception is not overriden, the invokable will
        be retried.
      strict: If true, an error will be raised when attempting to replay a
        subinvocation, but the provided subinvocation does not match the
        invokable and input. If false, a non-matching sub-invocation will
        be ignored and the corresponding invokable will be retried on its
        actual input.
    """
    super().__init__()
    self._exception_override = exception_override
    self._available_children = list(subinvocations)
    if not all(isinstance(x, references.Ref) for x in self._available_children):
      assert False
    self._strict = strict

  @classmethod
  def call_or_replay(
      cls, invokable: 'InvokableBase[I_contra, O_co]', arg: I_contra):
    """If there is a replay active, try to use it."""
    context: Optional[ReplayContext[I_contra, O_co]] = (
      ReplayContext.get_current())
    call = invokable.call

    if (isinstance(arg, interfaces.NoneResource) and
        len(inspect.signature(invokable.call).parameters) == 0):
      # Allow invokables that take no call args if they accept NoneResources.
      # pylint: disable=unnecessary-lambda-assignment
      call = lambda _: invokable.call()  # type: ignore

    if context:
      # pylint: disable=protected-access
      replayed_output, child_ctx = context._consume_replay(invokable, arg)
      if replayed_output is not None:
        return replayed_output
      else:
        with child_ctx:
          return call(arg)
    return call(arg)

  @classmethod
  async def async_call_or_replay(
      cls, invokable: 'AsyncInvokableBase[I_contra, O_co]', arg: I_contra):
    """If there is a replay active, try to use it."""
    context: Optional[ReplayContext[I_contra, O_co]] = (
      ReplayContext.get_current())
    call = invokable.call
    if (isinstance(arg, interfaces.NoneResource) and
        len(inspect.signature(invokable.call).parameters) == 0):
      # Allow invokables that take no call args if they accept NoneResources.
      # pylint: disable=unnecessary-lambda-assignment
      call = lambda _: invokable.call()  # type: ignore

    if context:
      # pylint: disable=protected-access
      replayed_output, child_ctx = context._consume_replay(invokable, arg)
      if replayed_output is not None:
        return replayed_output
      else:
        with child_ctx:
          result = await call(arg)
          return result
    result = await call(arg)
    return result

  def _consume_replay(
      self,
      invokable: '_InvokableBase[I_contra, O_co]',
      input_resource: I_contra) -> Tuple[Optional[O_co],
                                'ReplayContext[I_contra, O_co]']:
    """Replay the invocation if possible and return a child context."""
    request = references.commit(Request(
      references.commit(invokable),
      references.commit(input_resource)))
    for i, child in enumerate(self._available_children):
      if child().request == request:
        break
      elif self._strict:
        raise ReplayError(
          f'Expected invocation {invokable}({input_resource}) but got '
          f'{child().request().invokable()}({child().request().input()}).\n'
          f'Ensure that calls to subinvokable are deterministic '
          f'or use strict=False.')
    else:
      # No matching replay found.
      return None, ReplayContext([], self._exception_override)

    # Consume child invocation
    self._available_children.pop(i)
    replay_response = child().response()
    replay_children = replay_response.children

    # Replay successful executions.
    if replay_response.output:
      invokable.set_from(replay_response.invokable())
      Builder.register_replayed_subinvocations(replay_children)
      return replay_response.output(), ReplayContext(
        replay_children,
        self._exception_override,
        self._strict)

    # Check for exception override
    if replay_response.raised and replay_response.raised_here:
      # We only override exceptions at the point of the stack where they were
      # originally raised.
      override = self._exception_override(replay_response.raised)
      invokable.set_from(replay_response.invokable())
      if override is not None:
        # Typecheck the override.
        output_type = invokable.get_output_type()
        if output_type and not isinstance(override, output_type):
          raise InvokableTypeError(
            f'Exception override {override} is not of required type '
            f'{invokable.get_input_type()}.')
        Builder.register_replayed_subinvocations(replay_children)
        # Set invokable from response.
        return (
          cast(O_co, override),
          ReplayContext(replay_children,
                        self._exception_override,
                        self._strict))

    # Trigger reexecution of the invocation.
    return None, ReplayContext(
      replay_children,
      self._exception_override, self._strict)


class IncompleteSubinvocationError(InvocationError):
  """A subinvocation hasn't completed during the call."""


@contexts.register
class Builder(Generic[I_contra, O_co], contexts.Context):
  """A builder for invocations."""

  def __init__(
      self,
      invokable: '_InvokableBase[I_contra, O_co]',
      input_resource: references.Ref[I_contra]):
    """Initializes the builder."""
    super().__init__()
    self.invokable = invokable
    self.input_ref = input_resource

    self._children: Optional[List[Builder]] = None
    self._replayed_subinvocations: Optional[
      List[references.Ref[Invocation]]] = None
    self._request = Request(
      references.commit(invokable), input_resource)

    self._invocation: Optional[Invocation] = None
    self._parent: Optional[Builder] = self.get_current()
    if self._parent:
      self._parent.register_child(self)
    self.exception_raised: Optional[Exception] = None

  @property
  def completed(self) -> bool:
    """Returns true if the invocation is complete."""
    return self._invocation is not None

  def register_child(self, child: 'Builder'):
    """Registers a subinvocation."""
    if self._children is None:
      self._children = []
    self._children.append(child)

  @classmethod
  def register_replayed_subinvocations(
      cls, subinvocations: Iterable[references.Ref[Invocation]]):
    """Registers a list of subinvocations that were replayed."""
    builder = cls.get_current()
    if not builder:
      return
    # pylint: disable=protected-access
    builder._replayed_subinvocations = list(subinvocations)

  def _get_subinvocations(self) -> List[references.Ref[Invocation]]:
    """Returns the list of subinvocations."""
    assert (
      self._replayed_subinvocations is None or self._children is None), (
      'Cannot have both replayed and non-replayed subinvocations.')
    if self._replayed_subinvocations is not None:
      return self._replayed_subinvocations
    children = self._children or []
    for i, child in enumerate(children or []):
      if not child.completed:
        raise IncompleteSubinvocationError(
          f'Subinvocation {i} did not complete during invocation of parent:'
          f' {child.invokable} invoked on'
          f' {child.input_ref()}')
    return [references.commit(c.invocation) for c in children]

  def _is_child_exception(self, exception: Exception) -> bool:
    """Checks if the exception was originally raised by an immediate child."""
    children = self._children or []
    assert not self._replayed_subinvocations, (
      'Subinvocations were replayed, but an exception was raised.')
    return any(s.exception_raised is exception for s in children)

  @property
  def invocation(self) -> Invocation[I_contra, O_co]:
    assert self._invocation, (
      'The "call" function must be called before accessing the invocation.')
    return self._invocation

  def _check_call_valid(
      self,
      input_resource: interfaces.ResourceBase):
    """Checks that the call did not do invalid things."""
    if references.commit(input_resource) != self.input_ref:
      raise InputChanged(
        f'Input changed during invocation of {self.invokable} on input '
        f'{input_resource}. Only the invokable may change.')

  def _process_output(
      self,
      output_resource: Optional[interfaces.ResourceBase],
      ) -> references.Ref[O_co]:
    """Process the output and check for errors."""
    if output_resource is None:
      output_resource = interfaces.NoneResource()
    if not isinstance(output_resource, interfaces.ResourceBase):
      raise InvokableTypeError(
        f'Invokable {self.invokable} returned {output_resource} '
        f'which is not a resource.')
    return cast(references.Ref[O_co], references.commit(output_resource))

  def _wrap_exception(self, exception: Exception) -> ExceptionResource:
    """Wraps an exception if necessary."""
    if not isinstance(exception, ExceptionResource):
      return WrappedException(traceback.format_exc())
    return exception

  def _create_invocation(
      self,
      output_ref: Optional[references.Ref[O_co]],
      exception: Optional[Exception]):
    """Process a call result and set the invocation object."""
    exception_ref: Optional[references.Ref[ExceptionResource]] = None
    raised_here = False
    if exception:
      exception_ref = references.commit(
        self._wrap_exception(exception))
      self.exception_raised = exception
      raised_here = not self._is_child_exception(exception)
    subinvocations = self._get_subinvocations()
    response = Response(
      references.commit(self.invokable), output_ref,
      exception_ref, raised_here, children=subinvocations)
    self._invocation = Invocation(
      references.commit(self._request),
      references.commit(response))

  def call(self) -> O_co:
    """Call the invokable and set self.invocation."""
    with self:
      exception: Optional[Exception] = None
      output_ref: Optional[references.Ref[O_co]] = None
      try:
        input_resource = self.input_ref()
        invokable = self.invokable
        assert isinstance(invokable, InvokableBase)
        output_resource = ReplayContext.call_or_replay(
          invokable, input_resource)
        self._check_call_valid(input_resource)
        output_ref = self._process_output(output_resource)
      except Exception as e:
        exception = e
        raise
      finally:
        self._create_invocation(output_ref, exception)
      return cast(O_co, output_resource)

  async def async_call(self) -> O_co:
    """Call the async invokable and set self.invocation."""
    with self:
      exception: Optional[Exception] = None
      output_ref: Optional[references.Ref[O_co]] = None
      try:
        input_resource = self.input_ref()
        invokable = self.invokable
        assert isinstance(invokable, AsyncInvokableBase)
        output_resource = await ReplayContext.async_call_or_replay(
          invokable, input_resource)
        self._check_call_valid(input_resource)
        output_ref = self._process_output(output_resource)
      except Exception as e:
        exception = e
        raise
      finally:
        self._create_invocation(output_ref, exception)
      return cast(O_co, output_resource)


def resolve_resource(
    resource_type: Optional[Type[C]],
    *args, **kwargs):
  """Resolves resource from arguments."""
  input_type = resource_type
  if (len(args) != 1 or kwargs or
      not all(isinstance(arg, interfaces.ResourceBase)
              for arg in args) or
      not all(isinstance(arg, interfaces.ResourceBase)
              for arg in kwargs.values())):
    if input_type:
      # Attempts to create a resource of the input type.
      arg = input_type(*args, **kwargs)
    else:
      raise InvokableTypeError(
        f'Untyped invokables must be called with a single resource argument, '
        f'got args={args} and kwargs={kwargs}.')
  else:
    arg = args[0]

  if input_type and not isinstance(arg, input_type):
    raise InvokableTypeError(
      f'Input type {type(arg).__qualname__} does not match '
      f'{input_type.__qualname__}.')
  return arg


class _InvokableBase(Generic[I_contra, O_co], interfaces.ResourceBase):
  """Base class for sync / async invokable resources."""
  _input_type: Optional[Type[I_contra]] = None
  _output_type: Optional[Type[O_co]] = None

  @classmethod
  def get_input_type(cls) -> Optional[Type[I_contra]]:
    """Returns the type of the input if known."""
    return cls._input_type

  @classmethod
  def get_output_type(cls) -> Optional[Type[O_co]]:
    """Returns the type of the output if known."""
    return cls._output_type

  @staticmethod
  def _process_invoke_arg(
      arg: Optional[references.Ref[I_contra]]) -> (
        references.Ref[I_contra]):
    """Processes an invocation argument."""
    if arg is None:
      arg = cast(references.Ref[I_contra],
                 references.commit(interfaces.NoneResource()))
    if not isinstance(arg, references.Ref):
      raise InvokableTypeError('Input must be a reference.')
    return arg

  @staticmethod
  def _invoke_exit_stack(
      replay_from: Optional[Invocation[I_contra, O_co]]=None,
      exception_override: ExceptionOverride=lambda _: None,
      strict: bool=False) -> contextlib.ExitStack:
    """Creates an exit stack for invoking an invokable."""
    exit_stack = contextlib.ExitStack()
    # Execute in a top-level context to ensure that there are no parents.
    exit_stack.enter_context(Builder.top_level())
    if replay_from:
      exit_stack.enter_context(ReplayContext.top_level())
      exit_stack.enter_context(ReplayContext(
        [references.commit(replay_from)],
        exception_override, strict))
    return exit_stack


class InvokableBase(_InvokableBase[I_contra, O_co]):
  """Base class for invokable resources."""

  def call(self, resource: I_contra) -> O_co:
    """Executes the invokable."""
    raise NotImplementedError()

  def __call__(self, *args, **kwargs) -> O_co:
    """Executes the invokable, tracking invocation metadata."""
    arg = resolve_resource(self.get_input_type(), *args, **kwargs)
    parent: Optional[Builder] = Builder.get_current()

    # Execution not tracked, so just call the invokable.
    if not parent:
      return ReplayContext.call_or_replay(self, arg)

    builder = Builder(self, references.commit(arg))
    output = builder.call()
    return output

  def invoke(
      self,
      arg: Optional[references.Ref[I_contra]]=None,
      replay_from: Optional[Invocation[I_contra, O_co]]=None,
      exception_override: ExceptionOverride=lambda _: None,
      raise_on_invocation_error:bool=True,
      strict: bool=True,
      commit: bool=True) -> Invocation[I_contra, O_co]:
    """Invoke the invokable, tracking invocation metadata.

    Args:
      arg: The input resource.
      replay_from: An optional invocation to replay form.
      exception_override: If replaying, an optional override for replayed
        exceptions.
      raise_on_invocation_error: Whether invocation errors should be reraised.
      strict: Whether replay should fail if the replayed invocation
        does not match the current invocation.
      commit: Whether to commit the new invocation object.
    Returns:
      The invocation generated.
    """
    arg = self._process_invoke_arg(arg)

    with self._invoke_exit_stack(replay_from, exception_override, strict):
      builder = Builder(self, arg)
      try:
        builder.call()
      except InvocationError:
        if raise_on_invocation_error:
          raise
      except Exception:  # pylint: disable=broad-exception-caught
        if not builder.completed:
          raise
      invocation = builder.invocation
    if commit:
      references.commit(invocation)
    return invocation


class AsyncInvokableBase(_InvokableBase[I_contra, O_co]):
  """Base class for invokable resources."""

  async def call(self, resource: I_contra) -> O_co:
    """Executes the async invokable."""
    raise NotImplementedError()

  async def __call__(self, *args, **kwargs) -> O_co:
    """Executes the async invokable, tracking invocation metadata."""
    arg = resolve_resource(self.get_input_type(), *args, **kwargs)
    parent: Optional[Builder] = Builder.get_current()

    # Execution not tracked, so just call the invokable.
    if not parent:
      result = await ReplayContext.async_call_or_replay(self, arg)
      return result

    builder = Builder(self, references.commit(arg))
    output = await builder.async_call()
    return output

  async def invoke(
      self,
      arg: Optional[references.Ref[I_contra]]=None,
      replay_from: Optional[Invocation[I_contra, O_co]]=None,
      exception_override: ExceptionOverride=lambda _: None,
      raise_on_invocation_error:bool=True,
      strict: bool=True,
      commit: bool=True) -> Invocation[I_contra, O_co]:
    """Invoke the invokable, tracking invocation metadata.

    Args:
      arg: The input resource.
      replay_from: An optional invocation to replay form.
      exception_override: If replaying, an optional override for replayed
        exceptions.
      raise_on_invocation_error: Whether invocation errors should be reraised.
      strict: Whether replay should fail if the replayed invocation
        does not match the current invocation.
      commit: Whether to commit the new invocation object.
    Returns:
      The invocation generated.
    """
    arg = self._process_invoke_arg(arg)

    with self._invoke_exit_stack(replay_from, exception_override, strict):
      builder = Builder(self, arg)
      try:
        await builder.async_call()
      except InvocationError:
        if raise_on_invocation_error:
          raise
      except Exception:  # pylint: disable=broad-exception-caught
        if not builder.completed:
          raise
      invocation = builder.invocation
    if commit:
      references.commit(invocation)
    return invocation


@dataclasses.dataclass
class Invokable(InvokableBase[I_contra, O_co], resources.Resource):
  """Base class for dataclass-based invokable resources."""


@dataclasses.dataclass
class AsyncInvokable(AsyncInvokableBase[I_contra, O_co], resources.Resource):
  """Base class for dataclass-based async invokable resources."""


I = TypeVar('I', bound=_InvokableBase)


def typed_invokable(
    input_type: Type[I_contra],
    output_type: Type[O_co],
    register=True) -> Callable[
      [Type[I]], Type[I]]:
  """A decorator for creating typed invokables."""
  def _decorator(cls: Type[I]) -> Type[I]:
    """Decorates a class as a typed invokable."""
    if not issubclass(cls, _InvokableBase):
      raise TypeError('Invokable must be a subclass of InvokableBase.')
    # pylint: disable=protected-access
    cls._input_type = input_type
    cls._output_type = output_type
    if register:
      resource_registry.register(cls)
    return cls
  return _decorator


@resource_registry.register
@dataclasses.dataclass
class RequestInput(Invokable):
  """An invokable that raises an InputRequest."""
  requested_type: Type[interfaces.ResourceBase]
  context: Optional[interfaces.FieldValue] = None

  def call(self, resource: interfaces.ResourceBase) -> interfaces.ResourceBase:
    _request_input(resource, self.requested_type, self.context)
    assert False

def request_input(
    requested_type: Type[C],
    for_resource: Optional[interfaces.ResourceBase]=None,
    context: Optional[interfaces.FieldValue]=None) -> C:
  """Request an input from an external system / user.

  Args:
    requested_type: The type of input to request.
    for_resource: The resource to request input for. If unset, defaults to None.
    context: An optional context to provide to the input request, e.g.,
      instructions to a user.
  Returns:
    The requested input. Note that this function will not return a value
    during normal execution, but will raise an InputRequest exception,
    which can be used to resume the execution with an injected value.
  Raises:
    InputRequest: Raised to halt execution in order to await input.
  """
  if for_resource is None:
    for_resource = interfaces.NoneResource()
  return RequestInput(requested_type, context)(for_resource)


class InvocationGenerator(Generic[I_contra, O_co]):
  """A generator that yields InputRequests from an invocation."""

  def __init__(
      self,
      invokable: Optional[InvokableBase[I_contra, O_co]]=None,
      input_ref: Optional[references.Ref[I_contra]]=None,
      from_invocation: Optional[Invocation[I_contra, O_co]]=None):
    """Initializes an interactive invocation."""
    if invokable and not isinstance(invokable, InvokableBase):
      if isinstance(invokable, AsyncInvokableBase):
        raise TypeError(
          'AsyncInvokables cannot be used with InvocationGenerator.')
      raise TypeError('Invokable must be a child of SyncInvokableBase.')
    if from_invocation is not None and invokable is not None:
      raise ValueError(
        'Cannot specify both an invokable and an invocation.')
    if from_invocation is not None and input_ref is not None:
      raise ValueError(
        'Cannot specify both an input and an invocation.')
    self._invocation: Optional[Invocation[I_contra, O_co]] = None
    self._from_invocation = from_invocation
    self._invokable = invokable
    self._input = input_ref
    self._request_input: Optional[interfaces.ResourceBase] = None

  @property
  def complete(self) -> bool:
    """Whether the invocation is complete."""
    if not self._invocation:
      return False
    if self._invocation.successful():
      return True
    if not self._invocation.response().raised:
      return False
    return not isinstance(self._invocation.get_raised(), InputRequest)

  @property
  def invocation(self) -> Invocation[I_contra, O_co]:
    """The invocation."""
    if not self._invocation:
      raise InvocationError(
        'Invocation not started, please call next.')
    return self._invocation

  @property
  def input_request(self) -> InputRequest:
    """The current input request."""
    if self.complete:
      raise InvocationError('Invocation is complete.')
    input_request = self.invocation.get_raised()
    assert isinstance(input_request, InputRequest)
    return input_request

  def set_input(self, resource: interfaces.ResourceBase):
    """Set input for the next call to __next__.

    This allows using the generator in iterator-style, e.g.,

      for input_request in invocation_generator:
        input_request.set_input(...)

    Which can be more convenient than using send():

      input_request = next(invocation_generator)
      while True:
        try:
          input_request.send(...)
        except StopIteration:
          break

    Args:
      resource: The resource to set as input.
    """
    self._request_input = resource.deep_copy_resource()

  def __iter__(self) -> 'InvocationGenerator[I_contra, O_co]':
    """Returns the generator."""
    return self

  def __next__(self) -> InputRequest:
    """Continues the invocation until the next input request or completion."""
    if self.complete:
      raise StopIteration()

    if not self._invocation:
      if self._from_invocation:
        self._invocation = self._from_invocation.replay()
      else:
        assert self._invokable
        if not self._input:
          if interfaces.NoneResource != self._invokable.get_input_type():
            raise InvocationError(
              'Invokable does not have input type NoneResource. '
              'Please provide an explicit input reference on generator '
              'construction')
          self._input = cast(references.Ref[I_contra],
                             references.commit(interfaces.NoneResource()))
        self._invocation = self._invokable.invoke(self._input)
      if self.complete:
        raise StopIteration()
      return self.input_request
    else:
      if self._request_input is None:
        if not issubclass(interfaces.NoneResource,
                          self.input_request.requested_type):
          raise InvocationError(
            'Invocation requests non-None input. Please use \'send(...)\' '
            'instead or set the input using \'set_input(...)\'.')
        self._request_input = interfaces.NoneResource()
      self._invocation = self.input_request.continue_invocation(
        self._invocation, self._request_input)
      self._request_input = None
      if self.complete:
        raise StopIteration()
      return self.input_request

  def send(self, value) -> InputRequest:
    if not self._invocation:
      if value is not None:
        raise TypeError(
          'Can\'t send non-None value to a just-started generator.')
      return next(self)
    if self.complete:
      raise StopIteration()
    if not isinstance(value, self.input_request.requested_type):
      raise InvokableTypeError(
        f'Input type {type(value)} does not match requested type: '
        f'{self.input_request.requested_type}.')
    self._invocation = self.input_request.continue_invocation(
      self.invocation, value)
    if self.complete:
      raise StopIteration()
    return self.input_request

#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""SDK harness for executing Python Fns via the Fn API."""

# pytype: skip-file

from __future__ import annotations

import base64
import bisect
import collections
import copy
import heapq
import itertools
import json
import logging
import random
import threading
from dataclasses import dataclass
from dataclasses import field
from itertools import chain
from typing import TYPE_CHECKING
from typing import Any
from typing import Callable
from typing import Container
from typing import DefaultDict
from typing import Dict
from typing import FrozenSet
from typing import Iterable
from typing import Iterator
from typing import List
from typing import Mapping
from typing import MutableMapping
from typing import Optional
from typing import Set
from typing import Tuple
from typing import Type
from typing import TypeVar
from typing import Union
from typing import cast

from google.protobuf import duration_pb2
from google.protobuf import timestamp_pb2
from sortedcontainers import SortedDict
from sortedcontainers import SortedList

import apache_beam as beam
from apache_beam import coders
from apache_beam.coders import WindowedValueCoder
from apache_beam.coders import coder_impl
from apache_beam.internal import pickler
from apache_beam.io import iobase
from apache_beam.metrics import monitoring_infos
from apache_beam.portability import common_urns
from apache_beam.portability import python_urns
from apache_beam.portability.api import beam_fn_api_pb2
from apache_beam.portability.api import beam_runner_api_pb2
from apache_beam.runners import common
from apache_beam.runners import pipeline_context
from apache_beam.runners.worker import data_sampler
from apache_beam.runners.worker import operation_specs
from apache_beam.runners.worker import operations
from apache_beam.runners.worker import statesampler
from apache_beam.transforms import TimeDomain
from apache_beam.transforms import core
from apache_beam.transforms import environments
from apache_beam.transforms import sideinputs
from apache_beam.transforms import userstate
from apache_beam.transforms import window
from apache_beam.utils import counters
from apache_beam.utils import proto_utils
from apache_beam.utils import timestamp
from apache_beam.utils.windowed_value import WindowedValue

if TYPE_CHECKING:
  from google.protobuf import message  # pylint: disable=ungrouped-imports
  from apache_beam import pvalue
  from apache_beam.portability.api import metrics_pb2
  from apache_beam.runners.sdf_utils import SplitResultPrimary
  from apache_beam.runners.sdf_utils import SplitResultResidual
  from apache_beam.runners.worker import data_plane
  from apache_beam.runners.worker import sdk_worker
  from apache_beam.transforms.core import Windowing
  from apache_beam.transforms.window import BoundedWindow
  from apache_beam.utils import windowed_value

T = TypeVar('T')
ConstructorFn = Callable[[
    'BeamTransformFactory',
    Any,
    beam_runner_api_pb2.PTransform,
    Union['message.Message', bytes],
    Dict[str, List[operations.Operation]]
],
                         operations.Operation]
OperationT = TypeVar('OperationT', bound=operations.Operation)
FnApiUserRuntimeStateTypes = Union['ReadModifyWriteRuntimeState',
                                   'CombiningValueRuntimeState',
                                   'SynchronousSetRuntimeState',
                                   'SynchronousBagRuntimeState',
                                   'SynchronousOrderedListRuntimeState']

DATA_INPUT_URN = 'beam:runner:source:v1'
DATA_OUTPUT_URN = 'beam:runner:sink:v1'
SYNTHETIC_DATA_SAMPLING_URN = 'beam:internal:sampling:v1'
IDENTITY_DOFN_URN = 'beam:dofn:identity:0.1'
# TODO(vikasrk): Fix this once runner sends appropriate common_urns.
OLD_DATAFLOW_RUNNER_HARNESS_PARDO_URN = 'beam:dofn:javasdk:0.1'
OLD_DATAFLOW_RUNNER_HARNESS_READ_URN = 'beam:source:java:0.1'
URNS_NEEDING_PCOLLECTIONS = set([
    monitoring_infos.ELEMENT_COUNT_URN, monitoring_infos.SAMPLED_BYTE_SIZE_URN
])

_LOGGER = logging.getLogger(__name__)


class RunnerIOOperation(operations.Operation):
  """Common baseclass for runner harness IO operations."""
  def __init__(
      self,
      name_context: common.NameContext,
      step_name: Any,
      consumers: Mapping[Any, Iterable[operations.Operation]],
      counter_factory: counters.CounterFactory,
      state_sampler: statesampler.StateSampler,
      windowed_coder: coders.Coder,
      transform_id: str,
      data_channel: data_plane.DataChannel) -> None:
    super().__init__(name_context, None, counter_factory, state_sampler)
    self.windowed_coder = windowed_coder
    self.windowed_coder_impl = windowed_coder.get_impl()
    # transform_id represents the consumer for the bytes in the data plane for a
    # DataInputOperation or a producer of these bytes for a DataOutputOperation.
    self.transform_id = transform_id
    self.data_channel = data_channel
    for _, consumer_ops in consumers.items():
      for consumer in consumer_ops:
        self.add_receiver(consumer, 0)


class DataOutputOperation(RunnerIOOperation):
  """A sink-like operation that gathers outputs to be sent back to the runner.
  """
  def set_output_stream(
      self, output_stream: data_plane.ClosableOutputStream) -> None:
    self.output_stream = output_stream

  def process(self, windowed_value: windowed_value.WindowedValue) -> None:
    self.windowed_coder_impl.encode_to_stream(
        windowed_value, self.output_stream, True)
    self.output_stream.maybe_flush()

  def finish(self) -> None:
    super().finish()
    self.output_stream.close()


class DataInputOperation(RunnerIOOperation):
  """A source-like operation that gathers input from the runner."""
  def __init__(
      self,
      operation_name: common.NameContext,
      step_name,
      consumers: Mapping[Any, List[operations.Operation]],
      counter_factory: counters.CounterFactory,
      state_sampler: statesampler.StateSampler,
      windowed_coder: coders.Coder,
      transform_id,
      data_channel: data_plane.GrpcClientDataChannel) -> None:
    super().__init__(
        operation_name,
        step_name,
        consumers,
        counter_factory,
        state_sampler,
        windowed_coder,
        transform_id=transform_id,
        data_channel=data_channel)

    self.consumer = next(iter(consumers.values()))
    self.splitting_lock = threading.Lock()
    self.index = -1
    self.stop = float('inf')
    self.started = False

  def setup(self, data_sampler=None):
    super().setup(data_sampler)
    # We must do this manually as we don't have a spec or spec.output_coders.
    self.receivers = [
        operations.ConsumerSet.create(
            counter_factory=self.counter_factory,
            step_name=self.name_context.step_name,
            output_index=0,
            consumers=self.consumer,
            coder=self.windowed_coder,
            producer_type_hints=self._get_runtime_performance_hints(),
            producer_batch_converter=self.get_output_batch_converter())
    ]

  def start(self) -> None:
    super().start()
    with self.splitting_lock:
      self.started = True

  def process(self, windowed_value: windowed_value.WindowedValue) -> None:
    self.output(windowed_value)

  def process_encoded(self, encoded_windowed_values: bytes) -> None:
    input_stream = coder_impl.create_InputStream(encoded_windowed_values)
    while input_stream.size() > 0:
      with self.splitting_lock:
        if self.index == self.stop - 1:
          return
        self.index += 1
      try:
        decoded_value = self.windowed_coder_impl.decode_from_stream(
            input_stream, True)
      except Exception as exn:
        raise ValueError(
            "Error decoding input stream with coder " +
            str(self.windowed_coder)) from exn
      self.output(decoded_value)

  def monitoring_infos(
      self, transform_id: str, tag_to_pcollection_id: Dict[str, str]
  ) -> Dict[FrozenSet, metrics_pb2.MonitoringInfo]:
    all_monitoring_infos = super().monitoring_infos(
        transform_id, tag_to_pcollection_id)
    read_progress_info = monitoring_infos.int64_counter(
        monitoring_infos.DATA_CHANNEL_READ_INDEX,
        self.index,
        ptransform=transform_id)
    all_monitoring_infos[monitoring_infos.to_key(
        read_progress_info)] = read_progress_info
    return all_monitoring_infos

  # TODO(https://github.com/apache/beam/issues/19737): typing not compatible
  # with super type
  def try_split(  # type: ignore[override]
      self, fraction_of_remainder, total_buffer_size, allowed_split_points
  ) -> Optional[
      Tuple[
          int,
          Iterable[operations.SdfSplitResultsPrimary],
          Iterable[operations.SdfSplitResultsResidual],
          int]]:
    with self.splitting_lock:
      if not self.started:
        return None
      if self.index == -1:
        # We are "finished" with the (non-existent) previous element.
        current_element_progress = 1.0
      else:
        current_element_progress_object = (
            self.receivers[0].current_element_progress())
        if current_element_progress_object is None:
          current_element_progress = 0.5
        else:
          current_element_progress = (
              current_element_progress_object.fraction_completed)
      # Now figure out where to split.
      split = self._compute_split(
          self.index,
          current_element_progress,
          self.stop,
          fraction_of_remainder,
          total_buffer_size,
          allowed_split_points,
          self.receivers[0].try_split)
      if split:
        self.stop = split[-1]
      return split

  @staticmethod
  def _compute_split(
      index,
      current_element_progress,
      stop,
      fraction_of_remainder,
      total_buffer_size,
      allowed_split_points=(),
      try_split=lambda fraction: None):
    def is_valid_split_point(index):
      return not allowed_split_points or index in allowed_split_points

    if total_buffer_size < index + 1:
      total_buffer_size = index + 1
    elif total_buffer_size > stop:
      total_buffer_size = stop
    # The units here (except for keep_of_element_remainder) are all in
    # terms of number of (possibly fractional) elements.
    remainder = total_buffer_size - index - current_element_progress
    keep = remainder * fraction_of_remainder
    if current_element_progress < 1:
      keep_of_element_remainder = keep / (1 - current_element_progress)
      # If it's less than what's left of the current element,
      # try splitting at the current element.
      if (keep_of_element_remainder < 1 and is_valid_split_point(index) and
          is_valid_split_point(index + 1)):
        split: Optional[Tuple[
            Iterable[operations.SdfSplitResultsPrimary],
            Iterable[operations.SdfSplitResultsResidual]]] = try_split(
                keep_of_element_remainder)
        if split:
          element_primaries, element_residuals = split
          return index - 1, element_primaries, element_residuals, index + 1
    # Otherwise, split at the closest element boundary.
    # pylint: disable=bad-option-value
    stop_index = index + max(1, int(round(current_element_progress + keep)))
    if allowed_split_points and stop_index not in allowed_split_points:
      # Choose the closest allowed split point.
      allowed_split_points = sorted(allowed_split_points)
      closest = bisect.bisect(allowed_split_points, stop_index)
      if closest == 0:
        stop_index = allowed_split_points[0]
      elif closest == len(allowed_split_points):
        stop_index = allowed_split_points[-1]
      else:
        prev = allowed_split_points[closest - 1]
        next = allowed_split_points[closest]
        if index < prev and stop_index - prev < next - stop_index:
          stop_index = prev
        else:
          stop_index = next
    if index < stop_index < stop:
      return stop_index - 1, [], [], stop_index
    else:
      return None

  def finish(self) -> None:
    super().finish()
    with self.splitting_lock:
      self.index += 1
      self.started = False

  def reset(self) -> None:
    with self.splitting_lock:
      self.index = -1
      self.stop = float('inf')
    super().reset()


class _StateBackedIterable(object):
  def __init__(
      self,
      state_handler: sdk_worker.CachingStateHandler,
      state_key: beam_fn_api_pb2.StateKey,
      coder_or_impl: Union[coders.Coder, coder_impl.CoderImpl],
  ) -> None:
    self._state_handler = state_handler
    self._state_key = state_key
    if isinstance(coder_or_impl, coders.Coder):
      self._coder_impl = coder_or_impl.get_impl()
    else:
      self._coder_impl = coder_or_impl

  def __iter__(self) -> Iterator[Any]:
    return iter(
        self._state_handler.blocking_get(self._state_key, self._coder_impl))

  def __reduce__(self):
    return list, (list(self), )


coder_impl.FastPrimitivesCoderImpl.register_iterable_like_type(
    _StateBackedIterable)


class StateBackedSideInputMap(object):

  _BULK_READ_LIMIT = 100
  _BULK_READ_FULLY = "fully"
  _BULK_READ_PARTIALLY = "partially"

  def __init__(
      self,
      state_handler: sdk_worker.CachingStateHandler,
      transform_id: str,
      tag: Optional[str],
      side_input_data: pvalue.SideInputData,
      coder: WindowedValueCoder,
      use_bulk_read: bool = False,
  ) -> None:
    self._state_handler = state_handler
    self._transform_id = transform_id
    self._tag = tag
    self._side_input_data = side_input_data
    self._element_coder = coder.wrapped_value_coder
    self._target_window_coder = coder.window_coder
    # TODO(robertwb): Limit the cache size.
    self._cache: Dict[BoundedWindow, Any] = {}
    self._use_bulk_read = use_bulk_read

  def __getitem__(self, window):
    target_window = self._side_input_data.window_mapping_fn(window)
    if target_window not in self._cache:
      state_handler = self._state_handler
      access_pattern = self._side_input_data.access_pattern

      if access_pattern == common_urns.side_inputs.ITERABLE.urn:
        state_key = beam_fn_api_pb2.StateKey(
            iterable_side_input=beam_fn_api_pb2.StateKey.IterableSideInput(
                transform_id=self._transform_id,
                side_input_id=self._tag,
                window=self._target_window_coder.encode(target_window)))
        raw_view = _StateBackedIterable(
            state_handler, state_key, self._element_coder)

      elif access_pattern == common_urns.side_inputs.MULTIMAP.urn:
        state_key = beam_fn_api_pb2.StateKey(
            multimap_side_input=beam_fn_api_pb2.StateKey.MultimapSideInput(
                transform_id=self._transform_id,
                side_input_id=self._tag,
                window=self._target_window_coder.encode(target_window),
                key=b''))
        kv_iter_state_key = beam_fn_api_pb2.StateKey(
            multimap_keys_values_side_input=beam_fn_api_pb2.StateKey.
            MultimapKeysValuesSideInput(
                transform_id=self._transform_id,
                side_input_id=self._tag,
                window=self._target_window_coder.encode(target_window)))
        cache = {}
        key_coder = self._element_coder.key_coder()
        key_coder_impl = key_coder.get_impl()
        value_coder = self._element_coder.value_coder()
        use_bulk_read = self._use_bulk_read

        class MultiMap(object):
          _bulk_read = None
          _lock = threading.Lock()

          def __getitem__(self, key):
            if use_bulk_read:
              if self._bulk_read is None:
                with self._lock:
                  if self._bulk_read is None:
                    try:
                      # Attempt to bulk read the key-values over the iterable
                      # protocol which, if supported, can be much more efficient
                      # than point lookups if it fits into memory.
                      for ix, (k, vs) in enumerate(_StateBackedIterable(
                          state_handler,
                          kv_iter_state_key,
                          coders.TupleCoder(
                              (key_coder, coders.IterableCoder(value_coder))))):
                        cache[k] = vs
                        if ix > StateBackedSideInputMap._BULK_READ_LIMIT:
                          self._bulk_read = (
                              StateBackedSideInputMap._BULK_READ_PARTIALLY)
                          break
                      else:
                        # We reached the end of the iteration without breaking.
                        self._bulk_read = (
                            StateBackedSideInputMap._BULK_READ_FULLY)
                    except Exception:
                      _LOGGER.error(
                          "Iterable access of map side inputs unsupported.",
                          exc_info=True)
                      self._bulk_read = (
                          StateBackedSideInputMap._BULK_READ_PARTIALLY)

              if (self._bulk_read == StateBackedSideInputMap._BULK_READ_FULLY):
                return cache.get(key, [])

            if key not in cache:
              keyed_state_key = beam_fn_api_pb2.StateKey()
              keyed_state_key.CopyFrom(state_key)
              keyed_state_key.multimap_side_input.key = (
                  key_coder_impl.encode_nested(key))
              cache[key] = _StateBackedIterable(
                  state_handler, keyed_state_key, value_coder)

            return cache[key]

          def __reduce__(self):
            # TODO(robertwb): Figure out how to support this.
            raise TypeError(common_urns.side_inputs.MULTIMAP.urn)

        raw_view = MultiMap()

      else:
        raise ValueError("Unknown access pattern: '%s'" % access_pattern)

      self._cache[target_window] = self._side_input_data.view_fn(raw_view)
    return self._cache[target_window]

  def is_globally_windowed(self) -> bool:
    return (
        self._side_input_data.window_mapping_fn ==
        sideinputs._global_window_mapping_fn)

  def reset(self) -> None:
    # TODO(BEAM-5428): Cross-bundle caching respecting cache tokens.
    self._cache = {}


class ReadModifyWriteRuntimeState(userstate.ReadModifyWriteRuntimeState):
  def __init__(self, underlying_bag_state):
    self._underlying_bag_state = underlying_bag_state

  def read(self) -> Any:
    values = list(self._underlying_bag_state.read())
    if not values:
      return None
    return values[0]

  def write(self, value: Any) -> None:
    self.clear()
    self._underlying_bag_state.add(value)

  def clear(self) -> None:
    self._underlying_bag_state.clear()

  def commit(self) -> None:
    self._underlying_bag_state.commit()


class CombiningValueRuntimeState(userstate.CombiningValueRuntimeState):
  def __init__(
      self,
      underlying_bag_state: userstate.AccumulatingRuntimeState,
      combinefn: core.CombineFn) -> None:
    self._combinefn = combinefn
    self._combinefn.setup()
    self._underlying_bag_state = underlying_bag_state
    self._finalized = False

  def _read_accumulator(self, rewrite=True):
    merged_accumulator = self._combinefn.merge_accumulators(
        self._underlying_bag_state.read())
    if rewrite:
      self._underlying_bag_state.clear()
      self._underlying_bag_state.add(merged_accumulator)
    return merged_accumulator

  def read(self) -> Iterable[Any]:
    return self._combinefn.extract_output(self._read_accumulator())

  def add(self, value: Any) -> None:
    # Prefer blind writes, but don't let them grow unboundedly.
    # This should be tuned to be much lower, but for now exercise
    # both paths well.
    if random.random() < 0.5:
      accumulator = self._read_accumulator(False)
      self._underlying_bag_state.clear()
    else:
      accumulator = self._combinefn.create_accumulator()
    self._underlying_bag_state.add(
        self._combinefn.add_input(accumulator, value))

  def clear(self) -> None:
    self._underlying_bag_state.clear()

  def commit(self):
    self._underlying_bag_state.commit()

  def finalize(self):
    if not self._finalized:
      self._combinefn.teardown()
      self._finalized = True


class _ConcatIterable(object):
  """An iterable that is the concatination of two iterables.

  Unlike itertools.chain, this allows reiteration.
  """
  def __init__(self, first: Iterable[Any], second: Iterable[Any]) -> None:
    self.first = first
    self.second = second

  def __iter__(self) -> Iterator[Any]:
    for elem in self.first:
      yield elem
    for elem in self.second:
      yield elem


coder_impl.FastPrimitivesCoderImpl.register_iterable_like_type(_ConcatIterable)


class SynchronousBagRuntimeState(userstate.BagRuntimeState):
  def __init__(
      self,
      state_handler: sdk_worker.CachingStateHandler,
      state_key: beam_fn_api_pb2.StateKey,
      value_coder: coders.Coder) -> None:
    self._state_handler = state_handler
    self._state_key = state_key
    self._value_coder = value_coder
    self._cleared = False
    self._added_elements: List[Any] = []

  def read(self) -> Iterable[Any]:
    return _ConcatIterable([] if self._cleared else cast(
        'Iterable[Any]',
        _StateBackedIterable(
            self._state_handler, self._state_key, self._value_coder)),
                           self._added_elements)

  def add(self, value: Any) -> None:
    self._added_elements.append(value)

  def clear(self) -> None:
    self._cleared = True
    self._added_elements = []

  def commit(self) -> None:
    to_await = None
    if self._cleared:
      to_await = self._state_handler.clear(self._state_key)
    if self._added_elements:
      to_await = self._state_handler.extend(
          self._state_key, self._value_coder.get_impl(), self._added_elements)
    if to_await:
      # To commit, we need to wait on the last state request future to complete.
      to_await.get()


class SynchronousSetRuntimeState(userstate.SetRuntimeState):
  def __init__(
      self,
      state_handler: sdk_worker.CachingStateHandler,
      state_key: beam_fn_api_pb2.StateKey,
      value_coder: coders.Coder) -> None:
    self._state_handler = state_handler
    self._state_key = state_key
    self._value_coder = value_coder
    self._cleared = False
    self._added_elements: Set[Any] = set()

  def _compact_data(self, rewrite=True):
    accumulator = set(
        _ConcatIterable(
            set() if self._cleared else _StateBackedIterable(
                self._state_handler, self._state_key, self._value_coder),
            self._added_elements))

    if rewrite and accumulator:
      self._state_handler.clear(self._state_key)
      self._state_handler.extend(
          self._state_key, self._value_coder.get_impl(), accumulator)

      # Since everthing is already committed so we can safely reinitialize
      # added_elements here.
      self._added_elements = set()

    return accumulator

  def read(self) -> Set[Any]:
    return self._compact_data(rewrite=False)

  def add(self, value: Any) -> None:
    if self._cleared:
      # This is a good time explicitly clear.
      self._state_handler.clear(self._state_key)
      self._cleared = False

    self._added_elements.add(value)
    if random.random() > 0.5:
      self._compact_data()

  def clear(self) -> None:
    self._cleared = True
    self._added_elements = set()

  def commit(self) -> None:
    to_await = None
    if self._cleared:
      to_await = self._state_handler.clear(self._state_key)
    if self._added_elements:
      to_await = self._state_handler.extend(
          self._state_key, self._value_coder.get_impl(), self._added_elements)
    if to_await:
      # To commit, we need to wait on the last state request future to complete.
      to_await.get()


class RangeSet:
  """For Internal Use only. A simple range set for ranges of [x,y)."""
  def __init__(self) -> None:
    # The start points and end points are stored separately in order.
    self._sorted_starts = SortedList()
    self._sorted_ends = SortedList()

  def add(self, start: int, end: int) -> None:
    if start >= end:
      return

    # ranges[:min_idx] and ranges[max_idx:] is unaffected by this insertion
    # the first range whose end point >= the start of the new range
    min_idx = self._sorted_ends.bisect_left(start)
    # the first range whose start point > the end point of the new range
    max_idx = self._sorted_starts.bisect_right(end)

    if min_idx >= len(self._sorted_starts) or max_idx <= 0:
      # the new range is beyond any current ranges
      new_start = start
      new_end = end
    else:
      # the new range overlaps with ranges[min_idx:max_idx]
      new_start = min(start, self._sorted_starts[min_idx])
      new_end = max(end, self._sorted_ends[max_idx - 1])

      del self._sorted_starts[min_idx:max_idx]
      del self._sorted_ends[min_idx:max_idx]

    self._sorted_starts.add(new_start)
    self._sorted_ends.add(new_end)

  def __contains__(self, key: int) -> bool:
    idx = self._sorted_starts.bisect_left(key)
    return (idx < len(self._sorted_starts) and self._sorted_starts[idx]
            == key) or (idx > 0 and self._sorted_ends[idx - 1] > key)

  def __len__(self) -> int:
    assert len(self._sorted_starts) == len(self._sorted_ends)
    return len(self._sorted_starts)

  def __iter__(self) -> Iterator[Tuple[int, int]]:
    return zip(self._sorted_starts, self._sorted_ends)

  def __str__(self) -> str:
    return str(list(zip(self._sorted_starts, self._sorted_ends)))


class SynchronousOrderedListRuntimeState(userstate.OrderedListRuntimeState):
  RANGE_MIN = -(1 << 63)
  RANGE_MAX = (1 << 63) - 1
  TIMESTAMP_RANGE_MIN = timestamp.Timestamp(micros=RANGE_MIN)
  TIMESTAMP_RANGE_MAX = timestamp.Timestamp(micros=RANGE_MAX)

  def __init__(
      self,
      state_handler: sdk_worker.CachingStateHandler,
      state_key: beam_fn_api_pb2.StateKey,
      value_coder: coders.Coder) -> None:
    self._state_handler = state_handler
    self._state_key = state_key
    self._elem_coder = beam.coders.TupleCoder(
        [coders.VarIntCoder(), coders.coders.LengthPrefixCoder(value_coder)])
    self._cleared = False
    self._pending_adds = SortedDict()
    self._pending_removes = RangeSet()

  def add(self, elem: Tuple[timestamp.Timestamp, Any]) -> None:
    assert len(elem) == 2
    key_ts, value = elem
    key = key_ts.micros

    if key >= self.RANGE_MAX or key < self.RANGE_MIN:
      raise ValueError("key value %d is out of range" % key)
    self._pending_adds.setdefault(key, []).append(value)

  def read(self) -> Iterable[Tuple[timestamp.Timestamp, Any]]:
    return self.read_range(self.TIMESTAMP_RANGE_MIN, self.TIMESTAMP_RANGE_MAX)

  def read_range(
      self,
      min_timestamp: timestamp.Timestamp,
      limit_timestamp: timestamp.Timestamp
  ) -> Iterable[Tuple[timestamp.Timestamp, Any]]:
    # convert timestamp to int, as sort keys are stored as int internally.
    min_key = min_timestamp.micros
    limit_key = limit_timestamp.micros

    keys_to_add = self._pending_adds.irange(
        min_key, limit_key, inclusive=(True, False))

    # use list interpretation here to construct the actual list
    # of iterators of the selected range.
    local_items = chain.from_iterable([
        itertools.islice(
            zip(itertools.cycle([
                k,
            ]), self._pending_adds[k]),
            len(self._pending_adds[k])) for k in keys_to_add
    ])

    if not self._cleared:
      range_query_state_key = beam_fn_api_pb2.StateKey()
      range_query_state_key.CopyFrom(self._state_key)
      range_query_state_key.ordered_list_user_state.range.start = min_key
      range_query_state_key.ordered_list_user_state.range.end = limit_key

      # make a deep copy here because there could be other operations occur in
      # the middle of an iteration and change pending_removes
      pending_removes_snapshot = copy.deepcopy(self._pending_removes)
      persistent_items = filter(
          lambda kv: kv[0] not in pending_removes_snapshot,
          _StateBackedIterable(
              self._state_handler, range_query_state_key, self._elem_coder))

      return map(
          lambda x: (timestamp.Timestamp(micros=x[0]), x[1]),
          heapq.merge(persistent_items, local_items))

    return map(lambda x: (timestamp.Timestamp(micros=x[0]), x[1]), local_items)

  def clear(self) -> None:
    self._cleared = True
    self._pending_adds = SortedDict()
    self._pending_removes = RangeSet()
    self._pending_removes.add(self.RANGE_MIN, self.RANGE_MAX)

  def clear_range(
      self,
      min_timestamp: timestamp.Timestamp,
      limit_timestamp: timestamp.Timestamp) -> None:
    min_key = min_timestamp.micros
    limit_key = limit_timestamp.micros

    # materialize the keys to remove before the actual removal
    keys_to_remove = list(
        self._pending_adds.irange(min_key, limit_key, inclusive=(True, False)))
    for k in keys_to_remove:
      del self._pending_adds[k]

    if not self._cleared:
      self._pending_removes.add(min_key, limit_key)

  def commit(self) -> None:
    futures = []
    if self._pending_removes:
      for start, end in self._pending_removes:
        range_query_state_key = beam_fn_api_pb2.StateKey()
        range_query_state_key.CopyFrom(self._state_key)
        range_query_state_key.ordered_list_user_state.range.start = start
        range_query_state_key.ordered_list_user_state.range.end = end
        futures.append(self._state_handler.clear(range_query_state_key))

      self._pending_removes = RangeSet()

    if self._pending_adds:
      items_to_add = []
      for k in self._pending_adds:
        items_to_add.extend(zip(itertools.cycle([
            k,
        ]), self._pending_adds[k]))
      futures.append(
          self._state_handler.extend(
              self._state_key, self._elem_coder.get_impl(), items_to_add))
      self._pending_adds = SortedDict()

    if len(futures):
      # To commit, we need to wait on every state request futures to complete.
      for to_await in futures:
        to_await.get()

    self._cleared = False


class OutputTimer(userstate.BaseTimer):
  def __init__(
      self,
      key,
      window: BoundedWindow,
      timestamp: timestamp.Timestamp,
      paneinfo: windowed_value.PaneInfo,
      time_domain: str,
      timer_family_id: str,
      timer_coder_impl: coder_impl.TimerCoderImpl,
      output_stream: data_plane.ClosableOutputStream):
    self._key = key
    self._window = window
    self._input_timestamp = timestamp
    self._paneinfo = paneinfo
    self._time_domain = time_domain
    self._timer_family_id = timer_family_id
    self._output_stream = output_stream
    self._timer_coder_impl = timer_coder_impl

  def set(self, ts: timestamp.TimestampTypes, dynamic_timer_tag='') -> None:
    ts = timestamp.Timestamp.of(ts)
    timer = userstate.Timer(
        user_key=self._key,
        dynamic_timer_tag=dynamic_timer_tag,
        windows=(self._window, ),
        clear_bit=False,
        fire_timestamp=ts,
        hold_timestamp=ts if TimeDomain.is_event_time(self._time_domain) else
        self._input_timestamp,
        paneinfo=self._paneinfo)
    self._timer_coder_impl.encode_to_stream(timer, self._output_stream, True)
    self._output_stream.maybe_flush()

  def clear(self, dynamic_timer_tag='') -> None:
    timer = userstate.Timer(
        user_key=self._key,
        dynamic_timer_tag=dynamic_timer_tag,
        windows=(self._window, ),
        clear_bit=True,
        fire_timestamp=None,
        hold_timestamp=None,
        paneinfo=None)
    self._timer_coder_impl.encode_to_stream(timer, self._output_stream, True)
    self._output_stream.maybe_flush()


class TimerInfo(object):
  """A data class to store information related to a timer."""
  def __init__(self, timer_coder_impl, output_stream=None):
    self.timer_coder_impl = timer_coder_impl
    self.output_stream = output_stream


class FnApiUserStateContext(userstate.UserStateContext):
  """Interface for state and timers from SDK to Fn API servicer of state.."""
  def __init__(
      self,
      state_handler: sdk_worker.CachingStateHandler,
      transform_id: str,
      key_coder: coders.Coder,
      window_coder: coders.Coder,
  ) -> None:
    """Initialize a ``FnApiUserStateContext``.

    Args:
      state_handler: A StateServicer object.
      transform_id: The name of the PTransform that this context is associated.
      key_coder: Coder for the key type.
      window_coder: Coder for the window type.
    """
    self._state_handler = state_handler
    self._transform_id = transform_id
    self._key_coder = key_coder
    self._window_coder = window_coder
    # A mapping of {timer_family_id: TimerInfo}
    self._timers_info: Dict[str, TimerInfo] = {}
    self._all_states: Dict[tuple, FnApiUserRuntimeStateTypes] = {}

  def add_timer_info(self, timer_family_id: str, timer_info: TimerInfo) -> None:
    self._timers_info[timer_family_id] = timer_info

  def get_timer(
      self, timer_spec: userstate.TimerSpec, key, window, timestamp,
      pane) -> OutputTimer:
    assert self._timers_info[timer_spec.name].output_stream is not None
    timer_coder_impl = self._timers_info[timer_spec.name].timer_coder_impl
    output_stream = self._timers_info[timer_spec.name].output_stream
    return OutputTimer(
        key,
        window,
        timestamp,
        pane,
        timer_spec.time_domain,
        timer_spec.name,
        timer_coder_impl,
        output_stream)

  def get_state(self, *args: Any) -> FnApiUserRuntimeStateTypes:
    state_handle = self._all_states.get(args)
    if state_handle is None:
      state_handle = self._all_states[args] = self._create_state(*args)
    return state_handle

  def _create_state(
      self, state_spec: userstate.StateSpec, key,
      window: BoundedWindow) -> FnApiUserRuntimeStateTypes:
    if isinstance(state_spec,
                  (userstate.BagStateSpec,
                   userstate.CombiningValueStateSpec,
                   userstate.ReadModifyWriteStateSpec)):
      bag_state = SynchronousBagRuntimeState(
          self._state_handler,
          state_key=beam_fn_api_pb2.StateKey(
              bag_user_state=beam_fn_api_pb2.StateKey.BagUserState(
                  transform_id=self._transform_id,
                  user_state_id=state_spec.name,
                  window=self._window_coder.encode(window),
                  # State keys are expected in nested encoding format
                  key=self._key_coder.encode_nested(key))),
          value_coder=state_spec.coder)
      if isinstance(state_spec, userstate.BagStateSpec):
        return bag_state
      elif isinstance(state_spec, userstate.ReadModifyWriteStateSpec):
        return ReadModifyWriteRuntimeState(bag_state)
      else:
        return CombiningValueRuntimeState(
            bag_state, copy.deepcopy(state_spec.combine_fn))
    elif isinstance(state_spec, userstate.SetStateSpec):
      return SynchronousSetRuntimeState(
          self._state_handler,
          state_key=beam_fn_api_pb2.StateKey(
              bag_user_state=beam_fn_api_pb2.StateKey.BagUserState(
                  transform_id=self._transform_id,
                  user_state_id=state_spec.name,
                  window=self._window_coder.encode(window),
                  # State keys are expected in nested encoding format
                  key=self._key_coder.encode_nested(key))),
          value_coder=state_spec.coder)
    elif isinstance(state_spec, userstate.OrderedListStateSpec):
      return SynchronousOrderedListRuntimeState(
          self._state_handler,
          state_key=beam_fn_api_pb2.StateKey(
              ordered_list_user_state=beam_fn_api_pb2.StateKey.
              OrderedListUserState(
                  transform_id=self._transform_id,
                  user_state_id=state_spec.name,
                  window=self._window_coder.encode(window),
                  key=self._key_coder.encode_nested(key))),
          value_coder=state_spec.coder)
    else:
      raise NotImplementedError(state_spec)

  def commit(self) -> None:
    for state in self._all_states.values():
      state.commit()

  def reset(self) -> None:
    for state in self._all_states.values():
      state.finalize()
    self._all_states = {}


def memoize(func):
  cache = {}
  missing = object()

  def wrapper(*args):
    result = cache.get(args, missing)
    if result is missing:
      result = cache[args] = func(*args)
    return result

  return wrapper


def only_element(iterable: Iterable[T]) -> T:
  element, = iterable
  return element


def _environments_compatible(submission: str, runtime: str) -> bool:
  if submission == runtime:
    return True
  if 'rc' in submission and runtime in submission:
    # TODO(https://github.com/apache/beam/issues/28084): Loosen
    # the check for RCs until RC containers install the matching version.
    return True
  return False


def _verify_descriptor_created_in_a_compatible_env(
    process_bundle_descriptor: beam_fn_api_pb2.ProcessBundleDescriptor) -> None:

  runtime_sdk = environments.sdk_base_version_capability()
  for t in process_bundle_descriptor.transforms.values():
    env = process_bundle_descriptor.environments[t.environment_id]
    for c in env.capabilities:
      if (c.startswith(environments.SDK_VERSION_CAPABILITY_PREFIX) and
          not _environments_compatible(c, runtime_sdk)):
        raise RuntimeError(
            "Pipeline construction environment and pipeline runtime "
            "environment are not compatible. If you use a custom "
            "container image, check that the Python interpreter minor version "
            "and the Apache Beam version in your image match the versions "
            "used at pipeline construction time. "
            f"Submission environment: {c}. "
            f"Runtime environment: {runtime_sdk}.")

  # TODO: Consider warning on mismatches in versions of installed packages.


class BundleProcessor(object):
  """ A class for processing bundles of elements. """
  def __init__(
      self,
      runner_capabilities: FrozenSet[str],
      process_bundle_descriptor: beam_fn_api_pb2.ProcessBundleDescriptor,
      state_handler: sdk_worker.CachingStateHandler,
      data_channel_factory: data_plane.DataChannelFactory,
      data_sampler: Optional[data_sampler.DataSampler] = None,
  ) -> None:
    """Initialize a bundle processor.

    Args:
      runner_capabilities (``FrozenSet[str]``): The set of capabilities of the
        runner with which we will be interacting
      process_bundle_descriptor (``beam_fn_api_pb2.ProcessBundleDescriptor``):
        a description of the stage that this ``BundleProcessor``is to execute.
      state_handler (CachingStateHandler).
      data_channel_factory (``data_plane.DataChannelFactory``).
    """
    self.runner_capabilities = runner_capabilities
    self.process_bundle_descriptor = process_bundle_descriptor
    self.state_handler = state_handler
    self.data_channel_factory = data_channel_factory
    self.data_sampler = data_sampler
    self.current_instruction_id: Optional[str] = None
    # Represents whether the SDK is consuming received data.
    self.consuming_received_data = False

    _verify_descriptor_created_in_a_compatible_env(process_bundle_descriptor)
    # There is no guarantee that the runner only set
    # timer_api_service_descriptor when having timers. So this field cannot be
    # used as an indicator of timers.
    if self.process_bundle_descriptor.timer_api_service_descriptor.url:
      self.timer_data_channel = (
          data_channel_factory.create_data_channel_from_url(
              self.process_bundle_descriptor.timer_api_service_descriptor.url))
    else:
      self.timer_data_channel = None

    # A mapping of
    # {(transform_id, timer_family_id): TimerInfo}
    # The mapping is empty when there is no timer_family_specs in the
    # ProcessBundleDescriptor.
    self.timers_info: Dict[Tuple[str, str], TimerInfo] = {}

    # TODO(robertwb): Figure out the correct prefix to use for output counters
    # from StateSampler.
    self.counter_factory = counters.CounterFactory()
    self.state_sampler = statesampler.StateSampler(
        'fnapi-step-%s' % self.process_bundle_descriptor.id,
        self.counter_factory)

    self.ops = self.create_execution_tree(self.process_bundle_descriptor)
    for op in reversed(self.ops.values()):
      op.setup(self.data_sampler)
    self.splitting_lock = threading.Lock()

  def create_execution_tree(
      self, descriptor: beam_fn_api_pb2.ProcessBundleDescriptor
  ) -> collections.OrderedDict[str, operations.DoOperation]:
    transform_factory = BeamTransformFactory(
        self.runner_capabilities,
        descriptor,
        self.data_channel_factory,
        self.counter_factory,
        self.state_sampler,
        self.state_handler,
        self.data_sampler,
    )

    self.timers_info = transform_factory.extract_timers_info()

    def is_side_input(transform_proto, tag):
      if transform_proto.spec.urn == common_urns.primitives.PAR_DO.urn:
        return tag in proto_utils.parse_Bytes(
            transform_proto.spec.payload,
            beam_runner_api_pb2.ParDoPayload).side_inputs

    pcoll_consumers: DefaultDict[str, List[str]] = collections.defaultdict(list)
    for transform_id, transform_proto in descriptor.transforms.items():
      for tag, pcoll_id in transform_proto.inputs.items():
        if not is_side_input(transform_proto, tag):
          pcoll_consumers[pcoll_id].append(transform_id)

    @memoize
    def get_operation(transform_id: str) -> operations.Operation:
      transform_consumers = {
          tag: [get_operation(op) for op in pcoll_consumers[pcoll_id]]
          for tag, pcoll_id in
          descriptor.transforms[transform_id].outputs.items()
      }

      # Initialize transform-specific state in the Data Sampler.
      if self.data_sampler:
        self.data_sampler.initialize_samplers(
            transform_id, descriptor, transform_factory.get_coder)

      return transform_factory.create_operation(
          transform_id, transform_consumers)

    # Operations must be started (hence returned) in order.
    @memoize
    def topological_height(transform_id: str) -> int:
      return 1 + max([0] + [
          topological_height(consumer)
          for pcoll in descriptor.transforms[transform_id].outputs.values()
          for consumer in pcoll_consumers[pcoll]
      ])

    return collections.OrderedDict([(
        transform_id,
        cast(operations.DoOperation,
             get_operation(transform_id))) for transform_id in sorted(
                 descriptor.transforms, key=topological_height, reverse=True)])

  def reset(self) -> None:
    self.counter_factory.reset()
    self.state_sampler.reset()
    # Side input caches.
    for op in self.ops.values():
      op.reset()

  def process_bundle(
      self, instruction_id: str
  ) -> Tuple[List[beam_fn_api_pb2.DelayedBundleApplication], bool]:

    expected_input_ops: List[DataInputOperation] = []

    for op in self.ops.values():
      if isinstance(op, DataOutputOperation):
        # TODO(robertwb): Is there a better way to pass the instruction id to
        # the operation?
        op.set_output_stream(
            op.data_channel.output_stream(instruction_id, op.transform_id))
      elif isinstance(op, DataInputOperation):
        # We must wait until we receive "end of stream" for each of these ops.
        expected_input_ops.append(op)

    try:
      execution_context = ExecutionContext(instruction_id=instruction_id)
      self.current_instruction_id = instruction_id
      self.state_sampler.start()
      # Start all operations.
      for op in reversed(self.ops.values()):
        _LOGGER.debug('start %s', op)
        op.execution_context = execution_context
        op.start()

      # Each data_channel is mapped to a list of expected inputs which includes
      # both data input and timer input. The data input is identied by
      # transform_id. The data input is identified by
      # (transform_id, timer_family_id).
      data_channels: DefaultDict[data_plane.DataChannel,
                                 List[Union[str, Tuple[
                                     str,
                                     str]]]] = collections.defaultdict(list)

      # Add expected data inputs for each data channel.
      input_op_by_transform_id = {}
      for input_op in expected_input_ops:
        data_channels[input_op.data_channel].append(input_op.transform_id)
        input_op_by_transform_id[input_op.transform_id] = input_op

      # Update timer_data channel with expected timer inputs.
      if self.timer_data_channel:
        data_channels[self.timer_data_channel].extend(
            list(self.timers_info.keys()))

        # Set up timer output stream for DoOperation.
        for ((transform_id, timer_family_id),
             timer_info) in self.timers_info.items():
          output_stream = self.timer_data_channel.output_timer_stream(
              instruction_id, transform_id, timer_family_id)
          timer_info.output_stream = output_stream
          self.ops[transform_id].add_timer_info(timer_family_id, timer_info)

      # Process data and timer inputs
      # We are currently not consuming received data.
      self.consuming_received_data = False
      for data_channel, expected_inputs in data_channels.items():
        for element in data_channel.input_elements(instruction_id,
                                                   expected_inputs):
          # Since we have received a set of elements and are consuming it.
          self.consuming_received_data = True
          if isinstance(element, beam_fn_api_pb2.Elements.Timers):
            timer_coder_impl = (
                self.timers_info[(
                    element.transform_id,
                    element.timer_family_id)].timer_coder_impl)
            for timer_data in timer_coder_impl.decode_all(element.timers):
              self.ops[element.transform_id].process_timer(
                  element.timer_family_id, timer_data)
          elif isinstance(element, beam_fn_api_pb2.Elements.Data):
            input_op_by_transform_id[element.transform_id].process_encoded(
                element.data)
          # We are done consuming the set of elements.
          self.consuming_received_data = False

      # Finish all operations.
      for op in self.ops.values():
        _LOGGER.debug('finish %s', op)
        op.finish()

      # Close every timer output stream
      for timer_info in self.timers_info.values():
        assert timer_info.output_stream is not None
        timer_info.output_stream.close()

      return ([
          self.delayed_bundle_application(op, residual)
          for op, residual in execution_context.delayed_applications
      ],
              self.requires_finalization())

    finally:
      self.consuming_received_data = False
      # Ensure any in-flight split attempts complete.
      with self.splitting_lock:
        self.current_instruction_id = None
      self.state_sampler.stop_if_still_running()

  def finalize_bundle(self) -> beam_fn_api_pb2.FinalizeBundleResponse:
    for op in self.ops.values():
      op.finalize_bundle()
    return beam_fn_api_pb2.FinalizeBundleResponse()

  def requires_finalization(self) -> bool:
    return any(op.needs_finalization() for op in self.ops.values())

  def try_split(
      self, bundle_split_request: beam_fn_api_pb2.ProcessBundleSplitRequest
  ) -> beam_fn_api_pb2.ProcessBundleSplitResponse:
    split_response = beam_fn_api_pb2.ProcessBundleSplitResponse()
    with self.splitting_lock:
      if bundle_split_request.instruction_id != self.current_instruction_id:
        # This may be a delayed split for a former bundle, see BEAM-12475.
        return split_response

      for op in self.ops.values():
        if isinstance(op, DataInputOperation):
          desired_split = bundle_split_request.desired_splits.get(
              op.transform_id)
          if desired_split:
            split = op.try_split(
                desired_split.fraction_of_remainder,
                desired_split.estimated_input_elements,
                desired_split.allowed_split_points)
            if split:
              (
                  primary_end,
                  element_primaries,
                  element_residuals,
                  residual_start,
              ) = split
              for element_primary in element_primaries:
                split_response.primary_roots.add().CopyFrom(
                    self.bundle_application(*element_primary))
              for element_residual in element_residuals:
                split_response.residual_roots.add().CopyFrom(
                    self.delayed_bundle_application(*element_residual))
              split_response.channel_splits.extend([
                  beam_fn_api_pb2.ProcessBundleSplitResponse.ChannelSplit(
                      transform_id=op.transform_id,
                      last_primary_element=primary_end,
                      first_residual_element=residual_start)
              ])

    return split_response

  def delayed_bundle_application(
      self, op: operations.DoOperation, deferred_remainder: SplitResultResidual
  ) -> beam_fn_api_pb2.DelayedBundleApplication:
    assert op.input_info is not None
    # TODO(SDF): For non-root nodes, need main_input_coder + residual_coder.
    (element_and_restriction, current_watermark, deferred_timestamp) = (
        deferred_remainder)
    if deferred_timestamp:
      assert isinstance(deferred_timestamp, timestamp.Duration)
      proto_deferred_watermark: Optional[
          duration_pb2.Duration] = proto_utils.from_micros(
              duration_pb2.Duration, deferred_timestamp.micros)
    else:
      proto_deferred_watermark = None
    return beam_fn_api_pb2.DelayedBundleApplication(
        requested_time_delay=proto_deferred_watermark,
        application=self.construct_bundle_application(
            op.input_info, current_watermark, element_and_restriction))

  def bundle_application(
      self, op: operations.DoOperation,
      primary: SplitResultPrimary) -> beam_fn_api_pb2.BundleApplication:
    assert op.input_info is not None
    return self.construct_bundle_application(
        op.input_info, None, primary.primary_value)

  def construct_bundle_application(
      self,
      op_input_info: operations.OpInputInfo,
      output_watermark: Optional[timestamp.Timestamp],
      element) -> beam_fn_api_pb2.BundleApplication:
    transform_id, main_input_tag, main_input_coder, outputs = op_input_info
    if output_watermark:
      proto_output_watermark = proto_utils.from_micros(
          timestamp_pb2.Timestamp, output_watermark.micros)
      output_watermarks: Optional[Dict[str, timestamp_pb2.Timestamp]] = {
          output: proto_output_watermark
          for output in outputs
      }
    else:
      output_watermarks = None
    return beam_fn_api_pb2.BundleApplication(
        transform_id=transform_id,
        input_id=main_input_tag,
        output_watermarks=output_watermarks,
        element=main_input_coder.get_impl().encode_nested(element))

  def monitoring_infos(self) -> List[metrics_pb2.MonitoringInfo]:
    """Returns the list of MonitoringInfos collected processing this bundle."""
    # Construct a new dict first to remove duplicates.
    all_monitoring_infos_dict = {}
    for transform_id, op in self.ops.items():
      tag_to_pcollection_id = self.process_bundle_descriptor.transforms[
          transform_id].outputs
      all_monitoring_infos_dict.update(
          op.monitoring_infos(transform_id, dict(tag_to_pcollection_id)))

    return list(all_monitoring_infos_dict.values())

  def shutdown(self) -> None:
    for op in self.ops.values():
      op.teardown()


@dataclass
class ExecutionContext:
  # Any splits to be processed later.
  delayed_applications: List[Tuple[operations.DoOperation,
                                   common.SplitResultResidual]] = field(
                                       default_factory=list)

  # The exception sampler for the currently executing PTransform.
  output_sampler: Optional[data_sampler.OutputSampler] = None

  # The current instruction being executed.
  instruction_id: Optional[str] = None


class BeamTransformFactory(object):
  """Factory for turning transform_protos into executable operations."""
  def __init__(
      self,
      runner_capabilities: FrozenSet[str],
      descriptor: beam_fn_api_pb2.ProcessBundleDescriptor,
      data_channel_factory: data_plane.DataChannelFactory,
      counter_factory: counters.CounterFactory,
      state_sampler: statesampler.StateSampler,
      state_handler: sdk_worker.CachingStateHandler,
      data_sampler: Optional[data_sampler.DataSampler],
  ):
    self.runner_capabilities = runner_capabilities
    self.descriptor = descriptor
    self.data_channel_factory = data_channel_factory
    self.counter_factory = counter_factory
    self.state_sampler = state_sampler
    self.state_handler = state_handler
    self.context = pipeline_context.PipelineContext(
        descriptor,
        iterable_state_read=lambda token, element_coder_impl:
        _StateBackedIterable(
            state_handler, beam_fn_api_pb2.StateKey(
                runner=beam_fn_api_pb2.StateKey.Runner(key=token)),
            element_coder_impl))
    self.data_sampler = data_sampler

  _known_urns: Dict[str,
                    Tuple[ConstructorFn,
                          Union[Type[message.Message], Type[bytes],
                                None]]] = {}

  @classmethod
  def register_urn(
      cls, urn: str, parameter_type: Optional[Type[T]]
  ) -> Callable[[
      Callable[[
          BeamTransformFactory,
          str,
          beam_runner_api_pb2.PTransform,
          T,
          Dict[str, List[operations.Operation]]
      ],
               operations.Operation]
  ],
                Callable[[
                    BeamTransformFactory,
                    str,
                    beam_runner_api_pb2.PTransform,
                    T,
                    Dict[str, List[operations.Operation]]
                ],
                         operations.Operation]]:
    def wrapper(func):
      cls._known_urns[urn] = func, parameter_type
      return func

    return wrapper

  def create_operation(
      self, transform_id: str,
      consumers: Dict[str, List[operations.Operation]]) -> operations.Operation:
    transform_proto = self.descriptor.transforms[transform_id]
    if not transform_proto.unique_name:
      _LOGGER.debug("No unique name set for transform %s" % transform_id)
      transform_proto.unique_name = transform_id
    creator, parameter_type = self._known_urns[transform_proto.spec.urn]
    payload = proto_utils.parse_Bytes(
        transform_proto.spec.payload, parameter_type)
    return creator(self, transform_id, transform_proto, payload, consumers)

  def extract_timers_info(self) -> Dict[Tuple[str, str], TimerInfo]:
    timers_info = {}
    for transform_id, transform_proto in self.descriptor.transforms.items():
      if transform_proto.spec.urn == common_urns.primitives.PAR_DO.urn:
        pardo_payload = proto_utils.parse_Bytes(
            transform_proto.spec.payload, beam_runner_api_pb2.ParDoPayload)
        for (timer_family_id,
             timer_family_spec) in pardo_payload.timer_family_specs.items():
          timer_coder_impl = self.get_coder(
              timer_family_spec.timer_family_coder_id).get_impl()
          # The output_stream should be updated when processing a bundle.
          timers_info[(transform_id, timer_family_id)] = TimerInfo(
              timer_coder_impl=timer_coder_impl)
    return timers_info

  def get_coder(self, coder_id: str) -> coders.Coder:
    if coder_id not in self.descriptor.coders:
      raise KeyError("No such coder: %s" % coder_id)
    coder_proto = self.descriptor.coders[coder_id]
    if coder_proto.spec.urn:
      return self.context.coders.get_by_id(coder_id)
    else:
      # No URN, assume cloud object encoding json bytes.
      return operation_specs.get_coder_from_spec(
          json.loads(coder_proto.spec.payload.decode('utf-8')))

  def get_windowed_coder(self, pcoll_id: str) -> WindowedValueCoder:
    coder = self.get_coder(self.descriptor.pcollections[pcoll_id].coder_id)
    # TODO(robertwb): Remove this condition once all runners are consistent.
    if not isinstance(coder, WindowedValueCoder):
      windowing_strategy = self.descriptor.windowing_strategies[
          self.descriptor.pcollections[pcoll_id].windowing_strategy_id]
      return WindowedValueCoder(
          coder, self.get_coder(windowing_strategy.window_coder_id))
    else:
      return coder

  def get_output_coders(
      self, transform_proto: beam_runner_api_pb2.PTransform
  ) -> Dict[str, coders.Coder]:
    return {
        tag: self.get_windowed_coder(pcoll_id)
        for tag, pcoll_id in transform_proto.outputs.items()
    }

  def get_only_output_coder(
      self, transform_proto: beam_runner_api_pb2.PTransform) -> coders.Coder:
    return only_element(self.get_output_coders(transform_proto).values())

  def get_input_coders(
      self, transform_proto: beam_runner_api_pb2.PTransform
  ) -> Dict[str, coders.WindowedValueCoder]:
    return {
        tag: self.get_windowed_coder(pcoll_id)
        for tag, pcoll_id in transform_proto.inputs.items()
    }

  def get_only_input_coder(
      self, transform_proto: beam_runner_api_pb2.PTransform) -> coders.Coder:
    return only_element(list(self.get_input_coders(transform_proto).values()))

  def get_input_windowing(
      self, transform_proto: beam_runner_api_pb2.PTransform) -> Windowing:
    pcoll_id = only_element(transform_proto.inputs.values())
    windowing_strategy_id = self.descriptor.pcollections[
        pcoll_id].windowing_strategy_id
    return self.context.windowing_strategies.get_by_id(windowing_strategy_id)

  # TODO(robertwb): Update all operations to take these in the constructor.
  @staticmethod
  def augment_oldstyle_op(
      op: OperationT,
      step_name: str,
      consumers: Mapping[str, Iterable[operations.Operation]],
      tag_list: Optional[List[str]] = None) -> OperationT:
    op.step_name = step_name
    for tag, op_consumers in consumers.items():
      for consumer in op_consumers:
        op.add_receiver(consumer, tag_list.index(tag) if tag_list else 0)
    return op


@BeamTransformFactory.register_urn(
    DATA_INPUT_URN, beam_fn_api_pb2.RemoteGrpcPort)
def create_source_runner(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    grpc_port: beam_fn_api_pb2.RemoteGrpcPort,
    consumers: Dict[str, List[operations.Operation]]) -> DataInputOperation:

  output_coder = factory.get_coder(grpc_port.coder_id)
  return DataInputOperation(
      common.NameContext(transform_proto.unique_name, transform_id),
      transform_proto.unique_name,
      consumers,
      factory.counter_factory,
      factory.state_sampler,
      output_coder,
      transform_id=transform_id,
      data_channel=factory.data_channel_factory.create_data_channel(grpc_port))


@BeamTransformFactory.register_urn(
    DATA_OUTPUT_URN, beam_fn_api_pb2.RemoteGrpcPort)
def create_sink_runner(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    grpc_port: beam_fn_api_pb2.RemoteGrpcPort,
    consumers: Dict[str, List[operations.Operation]]) -> DataOutputOperation:
  output_coder = factory.get_coder(grpc_port.coder_id)
  return DataOutputOperation(
      common.NameContext(transform_proto.unique_name, transform_id),
      transform_proto.unique_name,
      consumers,
      factory.counter_factory,
      factory.state_sampler,
      output_coder,
      transform_id=transform_id,
      data_channel=factory.data_channel_factory.create_data_channel(grpc_port))


@BeamTransformFactory.register_urn(OLD_DATAFLOW_RUNNER_HARNESS_READ_URN, None)
def create_source_java(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    parameter,
    consumers: Dict[str,
                    List[operations.Operation]]) -> operations.ReadOperation:
  # The Dataflow runner harness strips the base64 encoding.
  source = pickler.loads(base64.b64encode(parameter))
  spec = operation_specs.WorkerRead(
      iobase.SourceBundle(1.0, source, None, None),
      [factory.get_only_output_coder(transform_proto)])
  return factory.augment_oldstyle_op(
      operations.ReadOperation(
          common.NameContext(transform_proto.unique_name, transform_id),
          spec,
          factory.counter_factory,
          factory.state_sampler),
      transform_proto.unique_name,
      consumers)


@BeamTransformFactory.register_urn(
    common_urns.deprecated_primitives.READ.urn, beam_runner_api_pb2.ReadPayload)
def create_deprecated_read(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    parameter: beam_runner_api_pb2.ReadPayload,
    consumers: Dict[str,
                    List[operations.Operation]]) -> operations.ReadOperation:
  source = iobase.BoundedSource.from_runner_api(
      parameter.source, factory.context)
  spec = operation_specs.WorkerRead(
      iobase.SourceBundle(1.0, source, None, None),
      [WindowedValueCoder(source.default_output_coder())])
  return factory.augment_oldstyle_op(
      operations.ReadOperation(
          common.NameContext(transform_proto.unique_name, transform_id),
          spec,
          factory.counter_factory,
          factory.state_sampler),
      transform_proto.unique_name,
      consumers)


@BeamTransformFactory.register_urn(
    python_urns.IMPULSE_READ_TRANSFORM, beam_runner_api_pb2.ReadPayload)
def create_read_from_impulse_python(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    parameter: beam_runner_api_pb2.ReadPayload,
    consumers: Dict[str, List[operations.Operation]]
) -> operations.ImpulseReadOperation:
  return operations.ImpulseReadOperation(
      common.NameContext(transform_proto.unique_name, transform_id),
      factory.counter_factory,
      factory.state_sampler,
      consumers,
      iobase.BoundedSource.from_runner_api(parameter.source, factory.context),
      factory.get_only_output_coder(transform_proto))


@BeamTransformFactory.register_urn(OLD_DATAFLOW_RUNNER_HARNESS_PARDO_URN, None)
def create_dofn_javasdk(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    serialized_fn,
    consumers: Dict[str, List[operations.Operation]]):
  return _create_pardo_operation(
      factory, transform_id, transform_proto, consumers, serialized_fn)


@BeamTransformFactory.register_urn(
    common_urns.sdf_components.PAIR_WITH_RESTRICTION.urn,
    beam_runner_api_pb2.ParDoPayload)
def create_pair_with_restriction(*args):
  class PairWithRestriction(beam.DoFn):
    def __init__(self, fn, restriction_provider, watermark_estimator_provider):
      self.restriction_provider = restriction_provider
      self.watermark_estimator_provider = watermark_estimator_provider

    def process(self, element, *args, **kwargs):
      # TODO(SDF): Do we want to allow mutation of the element?
      # (E.g. it could be nice to shift bulky description to the portion
      # that can be distributed.)
      initial_restriction = self.restriction_provider.initial_restriction(
          element)
      initial_estimator_state = (
          self.watermark_estimator_provider.initial_estimator_state(
              element, initial_restriction))
      yield (element, (initial_restriction, initial_estimator_state))

  return _create_sdf_operation(PairWithRestriction, *args)


@BeamTransformFactory.register_urn(
    common_urns.sdf_components.SPLIT_AND_SIZE_RESTRICTIONS.urn,
    beam_runner_api_pb2.ParDoPayload)
def create_split_and_size_restrictions(*args):
  class SplitAndSizeRestrictions(beam.DoFn):
    def __init__(self, fn, restriction_provider, watermark_estimator_provider):
      self.restriction_provider = restriction_provider
      self.watermark_estimator_provider = watermark_estimator_provider

    def process(self, element_restriction, *args, **kwargs):
      element, (restriction, _) = element_restriction
      for part, size in self.restriction_provider.split_and_size(
          element, restriction):
        if size < 0:
          raise ValueError('Expected size >= 0 but received %s.' % size)
        estimator_state = (
            self.watermark_estimator_provider.initial_estimator_state(
                element, part))
        yield ((element, (part, estimator_state)), size)

  return _create_sdf_operation(SplitAndSizeRestrictions, *args)


@BeamTransformFactory.register_urn(
    common_urns.sdf_components.TRUNCATE_SIZED_RESTRICTION.urn,
    beam_runner_api_pb2.ParDoPayload)
def create_truncate_sized_restriction(*args):
  class TruncateAndSizeRestriction(beam.DoFn):
    def __init__(self, fn, restriction_provider, watermark_estimator_provider):
      self.restriction_provider = restriction_provider

    def process(self, element_restriction, *args, **kwargs):
      ((element, (restriction, estimator_state)), _) = element_restriction
      truncated_restriction = self.restriction_provider.truncate(
          element, restriction)
      if truncated_restriction:
        truncated_restriction_size = (
            self.restriction_provider.restriction_size(
                element, truncated_restriction))
        if truncated_restriction_size < 0:
          raise ValueError(
              'Expected size >= 0 but received %s.' %
              truncated_restriction_size)
        yield ((element, (truncated_restriction, estimator_state)),
               truncated_restriction_size)

  return _create_sdf_operation(
      TruncateAndSizeRestriction,
      *args,
      operation_cls=operations.SdfTruncateSizedRestrictions)


@BeamTransformFactory.register_urn(
    common_urns.sdf_components.PROCESS_SIZED_ELEMENTS_AND_RESTRICTIONS.urn,
    beam_runner_api_pb2.ParDoPayload)
def create_process_sized_elements_and_restrictions(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    parameter: beam_runner_api_pb2.ParDoPayload,
    consumers: Dict[str, List[operations.Operation]]):
  return _create_pardo_operation(
      factory,
      transform_id,
      transform_proto,
      consumers,
      core.DoFnInfo.from_runner_api(parameter.do_fn,
                                    factory.context).serialized_dofn_data(),
      parameter,
      operation_cls=operations.SdfProcessSizedElements)


def _create_sdf_operation(
    proxy_dofn,
    factory,
    transform_id,
    transform_proto,
    parameter,
    consumers,
    operation_cls=operations.DoOperation):

  dofn_data = pickler.loads(parameter.do_fn.payload)
  dofn = dofn_data[0]
  restriction_provider = common.DoFnSignature(dofn).get_restriction_provider()
  watermark_estimator_provider = (
      common.DoFnSignature(dofn).get_watermark_estimator_provider())
  serialized_fn = pickler.dumps(
      (proxy_dofn(dofn, restriction_provider, watermark_estimator_provider), ) +
      dofn_data[1:])
  return _create_pardo_operation(
      factory,
      transform_id,
      transform_proto,
      consumers,
      serialized_fn,
      parameter,
      operation_cls=operation_cls)


@BeamTransformFactory.register_urn(
    common_urns.primitives.PAR_DO.urn, beam_runner_api_pb2.ParDoPayload)
def create_par_do(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    parameter: beam_runner_api_pb2.ParDoPayload,
    consumers: Dict[str, List[operations.Operation]]) -> operations.DoOperation:
  return _create_pardo_operation(
      factory,
      transform_id,
      transform_proto,
      consumers,
      core.DoFnInfo.from_runner_api(parameter.do_fn,
                                    factory.context).serialized_dofn_data(),
      parameter)


def _create_pardo_operation(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    consumers,
    serialized_fn,
    pardo_proto: Optional[beam_runner_api_pb2.ParDoPayload] = None,
    operation_cls=operations.DoOperation):

  if pardo_proto and pardo_proto.side_inputs:
    input_tags_to_coders = factory.get_input_coders(transform_proto)
    tagged_side_inputs = [
        (tag, beam.pvalue.SideInputData.from_runner_api(si, factory.context))
        for tag, si in pardo_proto.side_inputs.items()
    ]
    tagged_side_inputs.sort(
        key=lambda tag_si: sideinputs.get_sideinput_index(tag_si[0]))
    side_input_maps = [
        StateBackedSideInputMap(
            factory.state_handler,
            transform_id,
            tag,
            si,
            input_tags_to_coders[tag],
            use_bulk_read=(
                common_urns.runner_protocols.MULTIMAP_KEYS_VALUES_SIDE_INPUT.urn
                in factory.runner_capabilities))
        for (tag, si) in tagged_side_inputs
    ]
  else:
    side_input_maps = []

  output_tags = list(transform_proto.outputs.keys())

  dofn_data = pickler.loads(serialized_fn)
  if not dofn_data[-1]:
    # Windowing not set.
    if pardo_proto:
      other_input_tags: Container[str] = set.union(
          set(pardo_proto.side_inputs), set(pardo_proto.timer_family_specs))
    else:
      other_input_tags = ()
    pcoll_id, = [pcoll for tag, pcoll in transform_proto.inputs.items()
                 if tag not in other_input_tags]
    windowing = factory.context.windowing_strategies.get_by_id(
        factory.descriptor.pcollections[pcoll_id].windowing_strategy_id)
    serialized_fn = pickler.dumps(dofn_data[:-1] + (windowing, ))

  if pardo_proto and (pardo_proto.timer_family_specs or pardo_proto.state_specs
                      or pardo_proto.restriction_coder_id):
    found_input_coder = None
    for tag, pcoll_id in transform_proto.inputs.items():
      if tag in pardo_proto.side_inputs:
        pass
      else:
        # Must be the main input
        assert found_input_coder is None
        main_input_tag = tag
        found_input_coder = factory.get_windowed_coder(pcoll_id)
    assert found_input_coder is not None
    main_input_coder = found_input_coder

    if pardo_proto.timer_family_specs or pardo_proto.state_specs:
      user_state_context: Optional[
          FnApiUserStateContext] = FnApiUserStateContext(
              factory.state_handler,
              transform_id,
              main_input_coder.key_coder(),
              main_input_coder.window_coder)
    else:
      user_state_context = None
  else:
    user_state_context = None

  output_coders = factory.get_output_coders(transform_proto)
  spec = operation_specs.WorkerDoFn(
      serialized_fn=serialized_fn,
      output_tags=output_tags,
      input=None,
      side_inputs=None,  # Fn API uses proto definitions and the Fn State API
      output_coders=[output_coders[tag] for tag in output_tags])

  result = factory.augment_oldstyle_op(
      operation_cls(
          common.NameContext(transform_proto.unique_name, transform_id),
          spec,
          factory.counter_factory,
          factory.state_sampler,
          side_input_maps,
          user_state_context),
      transform_proto.unique_name,
      consumers,
      output_tags)
  if pardo_proto and pardo_proto.restriction_coder_id:
    result.input_info = operations.OpInputInfo(
        transform_id,
        main_input_tag,
        main_input_coder,
        transform_proto.outputs.keys())
  return result


def _create_simple_pardo_operation(
    factory: BeamTransformFactory,
    transform_id,
    transform_proto,
    consumers,
    dofn: beam.DoFn,
):
  serialized_fn = pickler.dumps((dofn, (), {}, [], None))
  return _create_pardo_operation(
      factory, transform_id, transform_proto, consumers, serialized_fn)


@BeamTransformFactory.register_urn(
    common_urns.primitives.ASSIGN_WINDOWS.urn,
    beam_runner_api_pb2.WindowingStrategy)
def create_assign_windows(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    parameter: beam_runner_api_pb2.WindowingStrategy,
    consumers: Dict[str, List[operations.Operation]]):
  class WindowIntoDoFn(beam.DoFn):
    def __init__(self, windowing):
      self.windowing = windowing

    def process(
        self,
        element,
        timestamp=beam.DoFn.TimestampParam,
        window=beam.DoFn.WindowParam):
      new_windows = self.windowing.windowfn.assign(
          WindowFn.AssignContext(timestamp, element=element, window=window))
      yield WindowedValue(element, timestamp, new_windows)

  from apache_beam.transforms.core import Windowing
  from apache_beam.transforms.window import WindowFn
  windowing = Windowing.from_runner_api(parameter, factory.context)
  return _create_simple_pardo_operation(
      factory,
      transform_id,
      transform_proto,
      consumers,
      WindowIntoDoFn(windowing))


@BeamTransformFactory.register_urn(IDENTITY_DOFN_URN, None)
def create_identity_dofn(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    parameter,
    consumers: Dict[str, List[operations.Operation]]
) -> operations.FlattenOperation:
  return factory.augment_oldstyle_op(
      operations.FlattenOperation(
          common.NameContext(transform_proto.unique_name, transform_id),
          operation_specs.WorkerFlatten(
              None, [factory.get_only_output_coder(transform_proto)]),
          factory.counter_factory,
          factory.state_sampler),
      transform_proto.unique_name,
      consumers)


@BeamTransformFactory.register_urn(
    common_urns.combine_components.COMBINE_PER_KEY_PRECOMBINE.urn,
    beam_runner_api_pb2.CombinePayload)
def create_combine_per_key_precombine(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    payload: beam_runner_api_pb2.CombinePayload,
    consumers: Dict[str,
                    List[operations.Operation]]) -> operations.PGBKCVOperation:
  serialized_combine_fn = pickler.dumps((
      beam.CombineFn.from_runner_api(payload.combine_fn,
                                     factory.context), [], {}))
  return factory.augment_oldstyle_op(
      operations.PGBKCVOperation(
          common.NameContext(transform_proto.unique_name, transform_id),
          operation_specs.WorkerPartialGroupByKey(
              serialized_combine_fn,
              None, [factory.get_only_output_coder(transform_proto)]),
          factory.counter_factory,
          factory.state_sampler,
          factory.get_input_windowing(transform_proto)),
      transform_proto.unique_name,
      consumers)


@BeamTransformFactory.register_urn(
    common_urns.combine_components.COMBINE_PER_KEY_MERGE_ACCUMULATORS.urn,
    beam_runner_api_pb2.CombinePayload)
def create_combbine_per_key_merge_accumulators(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    payload: beam_runner_api_pb2.CombinePayload,
    consumers: Dict[str, List[operations.Operation]]):
  return _create_combine_phase_operation(
      factory, transform_id, transform_proto, payload, consumers, 'merge')


@BeamTransformFactory.register_urn(
    common_urns.combine_components.COMBINE_PER_KEY_EXTRACT_OUTPUTS.urn,
    beam_runner_api_pb2.CombinePayload)
def create_combine_per_key_extract_outputs(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    payload: beam_runner_api_pb2.CombinePayload,
    consumers: Dict[str, List[operations.Operation]]):
  return _create_combine_phase_operation(
      factory, transform_id, transform_proto, payload, consumers, 'extract')


@BeamTransformFactory.register_urn(
    common_urns.combine_components.COMBINE_PER_KEY_CONVERT_TO_ACCUMULATORS.urn,
    beam_runner_api_pb2.CombinePayload)
def create_combine_per_key_convert_to_accumulators(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    payload: beam_runner_api_pb2.CombinePayload,
    consumers: Dict[str, List[operations.Operation]]):
  return _create_combine_phase_operation(
      factory, transform_id, transform_proto, payload, consumers, 'convert')


@BeamTransformFactory.register_urn(
    common_urns.combine_components.COMBINE_GROUPED_VALUES.urn,
    beam_runner_api_pb2.CombinePayload)
def create_combine_grouped_values(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    payload: beam_runner_api_pb2.CombinePayload,
    consumers: Dict[str, List[operations.Operation]]):
  return _create_combine_phase_operation(
      factory, transform_id, transform_proto, payload, consumers, 'all')


def _create_combine_phase_operation(
    factory, transform_id, transform_proto, payload, consumers,
    phase) -> operations.CombineOperation:
  serialized_combine_fn = pickler.dumps((
      beam.CombineFn.from_runner_api(payload.combine_fn,
                                     factory.context), [], {}))
  return factory.augment_oldstyle_op(
      operations.CombineOperation(
          common.NameContext(transform_proto.unique_name, transform_id),
          operation_specs.WorkerCombineFn(
              serialized_combine_fn,
              phase,
              None, [factory.get_only_output_coder(transform_proto)]),
          factory.counter_factory,
          factory.state_sampler),
      transform_proto.unique_name,
      consumers)


@BeamTransformFactory.register_urn(common_urns.primitives.FLATTEN.urn, None)
def create_flatten(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    payload,
    consumers: Dict[str, List[operations.Operation]]
) -> operations.FlattenOperation:
  return factory.augment_oldstyle_op(
      operations.FlattenOperation(
          common.NameContext(transform_proto.unique_name, transform_id),
          operation_specs.WorkerFlatten(
              None, [factory.get_only_output_coder(transform_proto)]),
          factory.counter_factory,
          factory.state_sampler),
      transform_proto.unique_name,
      consumers)


@BeamTransformFactory.register_urn(
    common_urns.primitives.MAP_WINDOWS.urn, beam_runner_api_pb2.FunctionSpec)
def create_map_windows(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    mapping_fn_spec: beam_runner_api_pb2.FunctionSpec,
    consumers: Dict[str, List[operations.Operation]]):
  assert mapping_fn_spec.urn == python_urns.PICKLED_WINDOW_MAPPING_FN
  window_mapping_fn = pickler.loads(mapping_fn_spec.payload)

  class MapWindows(beam.DoFn):
    def process(self, element):
      key, window = element
      return [(key, window_mapping_fn(window))]

  return _create_simple_pardo_operation(
      factory, transform_id, transform_proto, consumers, MapWindows())


@BeamTransformFactory.register_urn(
    common_urns.primitives.MERGE_WINDOWS.urn, beam_runner_api_pb2.FunctionSpec)
def create_merge_windows(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    mapping_fn_spec: beam_runner_api_pb2.FunctionSpec,
    consumers: Dict[str, List[operations.Operation]]):
  assert mapping_fn_spec.urn == python_urns.PICKLED_WINDOWFN
  window_fn = pickler.loads(mapping_fn_spec.payload)

  class MergeWindows(beam.DoFn):
    def process(self, element):
      nonce, windows = element

      original_windows: Set[window.BoundedWindow] = set(windows)
      merged_windows: MutableMapping[
          window.BoundedWindow,
          Set[window.BoundedWindow]] = collections.defaultdict(
              set)  # noqa: F821

      class RecordingMergeContext(window.WindowFn.MergeContext):
        def merge(
            self,
            to_be_merged: Iterable[window.BoundedWindow],
            merge_result: window.BoundedWindow,
        ):
          originals = merged_windows[merge_result]
          for w in to_be_merged:
            if w in original_windows:
              originals.add(w)
              original_windows.remove(w)
            else:
              originals.update(merged_windows.pop(w))

      window_fn.merge(RecordingMergeContext(windows))
      yield nonce, (original_windows, merged_windows.items())

  return _create_simple_pardo_operation(
      factory, transform_id, transform_proto, consumers, MergeWindows())


@BeamTransformFactory.register_urn(common_urns.primitives.TO_STRING.urn, None)
def create_to_string_fn(
    factory: BeamTransformFactory,
    transform_id: str,
    transform_proto: beam_runner_api_pb2.PTransform,
    mapping_fn_spec: beam_runner_api_pb2.FunctionSpec,
    consumers: Dict[str, List[operations.Operation]]):
  class ToString(beam.DoFn):
    def process(self, element):
      key, value = element
      return [(key, str(value))]

  return _create_simple_pardo_operation(
      factory, transform_id, transform_proto, consumers, ToString())

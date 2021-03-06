from __future__ import division

import os
import sys
import time
import threading
import re
import signal

from ..runtime import min_version, runtime_info
from ..profiler_scheduler import ProfilerScheduler
from ..metric import Metric
from ..metric import Breakdown
from ..frame import Frame

if runtime_info.GEVENT:
    import gevent


class BlockReporter:
    SAMPLING_RATE = 0.05
    MAX_TRACEBACK_SIZE = 25 # number of frames


    def __init__(self, agent):
        self.agent = agent
        self.profiler_scheduler = None
        self.profile_lock = threading.Lock()
        self.block_profile = None
        self.http_profile = None
        self.profile_duration = 0
        self.prev_signal_handler = None
        self.handler_active = False


    def start(self):
        if self.agent.get_option('block_profiler_disabled'):
            return

        if not runtime_info.OS_LINUX and not runtime_info.OS_DARWIN:
            self.agent.log('Time profiler is only supported on Linux and OS X.')
            return

        sample_time = self.SAMPLING_RATE * 1000

        main_thread_id = None
        if runtime_info.GEVENT:
            main_thread_id = gevent._threading.get_ident()
        else:
            main_thread_id = threading.current_thread().ident

        def _sample(signum, signal_frame):
            if self.handler_active:
                return
            self.handler_active = True

            with self.profile_lock:
                try:
                    if self.block_profile:
                        start = time.clock()

                        current_frames = sys._current_frames()
                        for thread_id, thread_frame in current_frames.items():
                            if thread_id == main_thread_id:
                                thread_frame = signal_frame

                            stack = self.recover_stack(thread_frame)
                            if stack:
                                self.update_block_profile(stack, sample_time)
                                self.update_http_profile(stack, sample_time)

                        self.block_profile._overhead += (time.clock() - start)
                except Exception:
                    self.agent.exception()

            self.handler_active = False

        self.prev_signal_handler = signal.signal(signal.SIGALRM, _sample)
        self.reset()

        self.profiler_scheduler = ProfilerScheduler(self.agent, 10, 2, 120, self.record, self.report)
        self.profiler_scheduler.start()


    def destroy(self):
        if self.agent.get_option('block_profiler_disabled'):
            return

        if self.prev_signal_handler:
            signal.signal(signal.SIGALRM, self.prev_signal_handler)

        if self.profiler_scheduler:
            self.profiler_scheduler.destroy()


    def reset(self):
        self.block_profile = Breakdown('root')
        self.http_profile = Breakdown('root')
        self.profile_duration = 0


    def record(self, duration):
        if self.agent.config.is_profiling_disabled():
            return

        signal.setitimer(signal.ITIMER_REAL, self.SAMPLING_RATE, self.SAMPLING_RATE)
        time.sleep(duration)
        signal.setitimer(signal.ITIMER_REAL, 0)

        self.profile_duration += duration

        self.agent.log('Time profiler CPU overhead per activity second: {0} seconds'.format(self.block_profile._overhead / self.profile_duration))


    def recover_stack(self, thread_frame):
        stack = []

        depth = 0
        while thread_frame is not None and depth <= self.MAX_TRACEBACK_SIZE:
            if thread_frame.f_code and thread_frame.f_code.co_name and thread_frame.f_code.co_filename:
                func_name = thread_frame.f_code.co_name
                filename = thread_frame.f_code.co_filename
                lineno = thread_frame.f_lineno

                if self.agent.frame_selector.is_agent_frame(filename):
                    return None

                if not self.agent.frame_selector.is_system_frame(filename):
                    frame = Frame(func_name, filename, lineno)
                    stack.append(frame)

                thread_frame = thread_frame.f_back
            
            depth += 1

        if len(stack) == 0:
            return None
        else:
            return stack


    def update_block_profile(self, stack, sample_time):
        current_node = self.block_profile
        current_node.increment(sample_time, 1)

        for frame in reversed(stack):
            current_node = current_node.find_or_add_child(str(frame))
            current_node.increment(sample_time, 1)


    def update_http_profile(self, stack, sample_time):
        include = False
        for frame in stack:
            if self.agent.frame_selector.is_http_frame(frame.filename):
                frame._skip = True
                include = True

        if include:
            current_node = self.http_profile
            current_node.increment(sample_time, 1)

            for frame in reversed(stack):
                if not frame._skip:
                    current_node = current_node.find_or_add_child(str(frame))
                    current_node.increment(sample_time, 1)


    def report(self):
        if self.agent.config.is_profiling_disabled():
            return

        if self.profile_duration == 0:
            return

        with self.profile_lock:
            self.block_profile.normalize(self.profile_duration)

            metric = Metric(self.agent, Metric.TYPE_PROFILE, Metric.CATEGORY_BLOCK_PROFILE, Metric.NAME_BLOCKING_CALL_TIMES, Metric.UNIT_MILLISECOND)
            measurement = metric.create_measurement(Metric.TRIGGER_TIMER, self.block_profile.measurement, 1, self.block_profile)
            self.agent.message_queue.add('metric', metric.to_dict())

            if self.block_profile.num_samples > 0 and self.http_profile.num_samples > 0:
                self.http_profile.normalize(self.profile_duration)
                self.http_profile.convert_to_percent(self.block_profile.measurement)

                metric = Metric(self.agent, Metric.TYPE_PROFILE, Metric.CATEGORY_HTTP_TRACE, Metric.NAME_HTTP_TRANSACTION_BREAKDOWN, Metric.UNIT_PERCENT)
                measurement = metric.create_measurement(Metric.TRIGGER_TIMER, self.http_profile.measurement, None, self.http_profile)
                self.agent.message_queue.add('metric', metric.to_dict())

        self.reset()
